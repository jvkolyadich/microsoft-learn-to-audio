#!/usr/bin/env python3
"""Record the audio polish pass as replayable edits, and replay it.

``strip_for_audio.py`` regenerates the ``-audio`` tree from scratch, and
``content/`` is not in version control, so every improvement to the stripper
would otherwise throw away the polish pass that followed it. This script
keeps that work:

    record   diff a polished tree against a freshly stripped one and write
             every difference out as a {file, find, replace} edit
    replay   apply those edits to a freshly stripped tree, writing the ones
             that no longer fit out as sites to re-polish

The ledger is derived from the diff rather than from what the polish workers
say they did: a worker's report is a claim, the diff is what happened.

``find`` must occur exactly once in its file -- the same rule the Claude Code
Edit tool enforces -- so ``record`` widens the context around each change
until it is unique. On replay, zero or several occurrences is a *miss*: the
edit is skipped and written to ``--misses`` in the same batch format
``strip_for_audio.py --report`` uses, never guessed at. A changed stripper
changes text at exactly the sites that need re-polishing, so misses are not a
failure of replay; they are its output.

Every artifact of the pass -- the ledger, the sites, the misses -- lives in
``content/<course>-polish/``, beside the tree and not inside it: step 2
rewrites the ``-audio`` tree from scratch. Being under ``content/`` also keeps
it out of git, which matters because the entries quote Learn prose verbatim.

Examples:
    python polish_ledger.py record content/dp-600t00-audio content/dp-600t00-fresh
    python polish_ledger.py record <polished> <fresh> -o some/other/ledger.json
    python polish_ledger.py replay content/dp-600t00-polish/polish-edits.json content/dp-600t00-audio
    python polish_ledger.py replay <ledger> <tree> --misses some/other/dir
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path


def long_path(path: Path) -> Path:
    """Windows refuses paths over 260 characters without this prefix."""
    if os.name != "nt":
        return path
    full = os.path.abspath(str(path))
    if full.startswith("\\\\?\\"):
        return Path(full)
    if full.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + full[2:])
    return Path("\\\\?\\" + full)


def polish_dir(tree: Path) -> Path:
    """Where one course's polish artifacts live: ``content/<course>-polish/``.

    Beside the tree rather than inside it, because ``strip_for_audio.py``
    rewrites the ``-audio`` tree from scratch and people delete it to force
    that. Not in ``.cache/`` either: that holds the refetchable catalog and
    anything in a directory called cache is one cleanup away from being gone,
    while the ledger is the only record of an LLM pass that cannot be
    reproduced. Under ``content/`` it is also gitignored, which it must be --
    ``find`` and ``replace`` carry verbatim Microsoft Learn prose.
    """
    name = tree.name[:-len("-audio")] if tree.name.endswith("-audio") else tree.name
    return tree.parent / (name + "-polish")


def front_matter_lines(lines: "list[str]") -> int:
    """How many leading lines are YAML front matter.

    Front matter is a contract: ``build_epub.py`` reads the unit title from
    it. The ledger neither records changes inside it nor widens context
    across it.
    """
    if not lines or lines[0].strip() != "---":
        return 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i + 1
    return 0


def md_files(root: Path) -> "dict[str, Path]":
    scan = long_path(root)
    return {str(p.relative_to(scan)).replace(os.sep, "/"): p
            for p in sorted(scan.rglob("*.md")) if p.is_file()}


# --------------------------------------------------------------------------
# record
# --------------------------------------------------------------------------


def changes(fresh: "list[str]", polished: "list[str]", gap: int) -> "list[tuple]":
    """Non-equal opcodes as ``(i1, i2, j1, j2)``, merging any two that sit
    less than *gap* unchanged lines apart.

    Merging is how uniqueness is bought when one line of context is not
    enough: two edits a line apart become one edit whose text spans both.
    """
    ops = [op[1:] for op in difflib.SequenceMatcher(None, fresh, polished).get_opcodes()
           if op[0] != "equal"]
    merged: "list[list[int]]" = []
    for i1, i2, j1, j2 in ops:
        if merged and i1 - merged[-1][1] <= gap:
            merged[-1][1], merged[-1][3] = i2, j2
        else:
            merged.append([i1, i2, j1, j2])
    return [tuple(m) for m in merged]


def unique_span(lines: "list[str]", text: str, start: int, end: int,
                floor: int, ceiling: int):
    """Widen ``lines[start:end]`` until it appears exactly once in *text*.

    Widens inside the paragraph first and only then across blank lines: a
    ``find`` that reaches into the next paragraph is still correct to replay,
    but it reads as if the worker had edited lines it never touched.
    ``floor``/``ceiling`` keep the span inside the unchanged runs on either
    side, so the matching span in the polished file can be derived from it.
    Returns ``(start, end)`` or ``None`` when no unique span exists in range.
    """
    lo, hi = max(floor, start - 1), min(ceiling, end + 1)
    for cross_blanks in (False, True):
        while True:
            find = "\n".join(lines[lo:hi])
            if find.strip() and text.count(find) == 1:
                return lo, hi
            up = lo > floor and (cross_blanks or lines[lo - 1].strip())
            down = hi < ceiling and (cross_blanks or lines[hi].strip())
            if not up and not down:
                break
            lo -= 1 if up else 0
            hi += 1 if down else 0
    return None


def file_entries(name: str, fresh_text: str, polished_text: str,
                 counts) -> "list[dict]":
    """Every difference between one unit's two versions, as ledger entries."""
    fresh = fresh_text.splitlines()
    polished = polished_text.splitlines()
    front = front_matter_lines(fresh)

    gap = 1
    while True:
        spans = changes(fresh, polished, gap)
        entries: "list[dict]" = []
        stuck = False
        for index, (i1, i2, j1, j2) in enumerate(spans):
            if i1 < front or i2 <= front:
                # Nothing may edit front matter; say so rather than record it.
                print("  ! %s: change inside front matter at line %d, not recorded"
                      % (name, i1 + 1), file=sys.stderr)
                counts["front_matter"] += 1
                continue
            floor = max(front, spans[index - 1][1] if index else 0)
            ceiling = spans[index + 1][0] if index + 1 < len(spans) else len(fresh)
            span = unique_span(fresh, fresh_text, i1, i2, floor, ceiling)
            if not span:
                stuck = True
                break
            lo, hi = span
            entries.append({
                "file": name,
                "line": lo + 1,
                "find": "\n".join(fresh[lo:hi]),
                "replace": "\n".join(polished[j1 - (i1 - lo):j2 + (hi - i2)]),
            })
        if not stuck:
            return entries
        if gap >= len(fresh):
            print("  ! %s: no unique anchor for one change, not recorded" % name,
                  file=sys.stderr)
            counts["unanchored"] += 1
            return entries
        gap = gap * 2 + 1  # merge more aggressively and try again


def record(polished_root: Path, fresh_root: Path, out: Path) -> int:
    polished_files = md_files(polished_root)
    fresh_files = md_files(fresh_root)

    counts = {"front_matter": 0, "unanchored": 0, "failed": 0}
    entries: "list[dict]" = []
    changed = 0
    for name, fresh_path in fresh_files.items():
        polished_path = polished_files.get(name)
        if polished_path is None:
            print("  ! only in %s: %s" % (fresh_root, name), file=sys.stderr)
            continue
        try:
            fresh_text = fresh_path.read_text(encoding="utf-8")
            polished_text = polished_path.read_text(encoding="utf-8")
            if fresh_text == polished_text:
                continue
            entries.extend(file_entries(name, fresh_text, polished_text, counts))
        except (OSError, ValueError) as exc:  # one bad unit must not stop the run
            counts["failed"] += 1
            print("  ! %s: %s" % (name, exc), file=sys.stderr)
            continue
        changed += 1
    for name in polished_files:
        if name not in fresh_files:
            print("  ! only in %s: %s" % (polished_root, name), file=sys.stderr)

    with open(long_path(out), "w", encoding="utf-8") as handle:
        json.dump({"polished": str(polished_root), "fresh": str(fresh_root),
                   "edits": entries}, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    print("Recorded %d edit(s) across %d unit(s) -> %s" % (len(entries), changed, out))
    if counts["front_matter"]:
        print("  %-24s %d" % ("front matter skipped:", counts["front_matter"]))
    if counts["unanchored"]:
        print("  %-24s %d" % ("unanchored skipped:", counts["unanchored"]))
    if counts["failed"]:
        print("  %-24s %d" % ("unreadable units:", counts["failed"]))
    return 0


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


def write_misses(misses: "list[dict]", report_dir: Path, batch_size: int) -> int:
    """Write missed edits as sites, in ``--report``'s batch format.

    Same shape, so the polish skill can hand a batch of misses to a worker
    without knowing where it came from: the old ``find`` is the context the
    worker has to relocate, the old ``replace`` is what it turned into.
    """
    root = long_path(report_dir)
    root.mkdir(parents=True, exist_ok=True)
    for stale in sorted(root.glob("batch-*.json")):
        stale.unlink()  # a re-run must not leave the last run's batches behind

    batches: "list[list[dict]]" = []
    current: "list[dict]" = []
    units = 0
    previous = None
    for miss in misses:
        if miss["file"] != previous:
            if units >= batch_size:
                batches.append(current)
                current, units = [], 0
            previous = miss["file"]
            units += 1
        current.append(miss)
    if current:
        batches.append(current)

    for number, batch in enumerate(batches, start=1):
        with open(root / ("batch-%02d.json" % number), "w", encoding="utf-8") as handle:
            json.dump(batch, handle, indent=1, ensure_ascii=False)
            handle.write("\n")
    return len(batches)


def replay(ledger_path: Path, tree: Path, misses_dir: Path, batch_size: int) -> int:
    with open(long_path(ledger_path), encoding="utf-8") as handle:
        ledger = json.load(handle)
    edits = ledger["edits"] if isinstance(ledger, dict) else ledger

    # manifest.json is the authoritative ordering for build_epub.py and
    # strip_for_audio.py only copies it when it mirrors a tree. A tree without
    # one is not a stripped tree, and replaying into it would be a mistake.
    if not os.path.exists(long_path(tree / "manifest.json")):
        print("No manifest.json in %s -- is that a stripped tree?" % tree, file=sys.stderr)
        return 2

    texts: "dict[str, str]" = {}
    applied = 0
    misses: "list[dict]" = []
    for edit in edits:
        name = edit["file"]
        path = long_path(tree / name)
        if name not in texts:
            try:
                texts[name] = path.read_text(encoding="utf-8")
            except OSError as exc:
                print("  ! %s: %s" % (name, exc), file=sys.stderr)
                texts[name] = ""
        text = texts[name]
        if text.count(edit["find"]) == 1:
            texts[name] = text.replace(edit["find"], edit["replace"], 1)
            applied += 1
            continue
        # Zero matches or several: the text moved or repeated. Never guess.
        misses.append({"file": name, "line": edit.get("line", 0),
                       "class": "ledger-miss", "removed": edit["replace"],
                       "context": edit["find"]})

    touched = 0
    for name, text in texts.items():
        path = long_path(tree / name)
        try:
            if text and text != path.read_text(encoding="utf-8"):
                path.write_text(text, encoding="utf-8")
                touched += 1
        except OSError as exc:
            print("  ! %s: %s" % (name, exc), file=sys.stderr)

    print("Replayed %s -> %s" % (ledger_path, tree))
    print("  %-24s %d" % ("applied:", applied))
    print("  %-24s %d" % ("missed:", len(misses)))
    print("  %-24s %d" % ("files touched:", touched))
    if misses:
        batches = write_misses(misses, misses_dir, max(1, batch_size))
        print("  %-24s %s (%d batch file(s))" % ("misses written to:", misses_dir, batches))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record the audio polish pass as replayable edits, and replay it.")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="Diff a polished tree against a fresh one")
    rec.add_argument("polished", type=Path, help="The polished -audio tree")
    rec.add_argument("fresh", type=Path, help="The same tree, freshly stripped")
    rec.add_argument("-o", "--out", type=Path, default=None,
                     help="Ledger to write (default: <polished>/../<course>-polish/"
                          "polish-edits.json)")

    rep = sub.add_parser("replay", help="Apply a ledger to a freshly stripped tree")
    rep.add_argument("ledger", type=Path, help="Ledger written by record")
    rep.add_argument("tree", type=Path, help="Tree to apply it to, edited in place")
    rep.add_argument("--misses", type=Path, default=None, metavar="DIR",
                     help="Where to write edits that no longer fit, as batched "
                          "sites (default: <tree>/../<course>-polish/misses)")
    rep.add_argument("--batch-size", type=int, default=8, metavar="N",
                     help="Units per miss batch file (default: 8)")
    args = parser.parse_args()

    if args.command == "record":
        for root in (args.polished, args.fresh):
            if not root.is_dir():
                print("Not a directory: %s" % root, file=sys.stderr)
                return 2
        polished = args.polished.resolve()
        out = args.out or polish_dir(polished) / "polish-edits.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        return record(polished, args.fresh.resolve(), out)

    if not args.ledger.is_file():
        print("No such ledger: %s" % args.ledger, file=sys.stderr)
        return 2
    if not args.tree.is_dir():
        print("Not a directory: %s" % args.tree, file=sys.stderr)
        return 2
    tree = args.tree.resolve()
    misses = args.misses or polish_dir(tree) / "misses"
    return replay(args.ledger, tree, misses, args.batch_size)


if __name__ == "__main__":
    sys.exit(main())
