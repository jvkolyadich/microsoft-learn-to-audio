#!/usr/bin/env python3
"""Strip Markdown of content that does not work when read aloud.

Walks a tree of ``.md`` files (the output of ``download_course.py``) and
writes a cleaned mirror of it. Removed or rewritten by default:

  * fenced code blocks                -> dropped
  * images ``![alt](url)`` and <img>  -> dropped
  * ``[Video: ...]`` markers          -> dropped
  * links ``[text](url)``             -> just ``text``
  * bare URLs and autolinks           -> dropped
  * inline code ``like this``         -> the text without backticks
  * tab strips (``#tabpanel_`` links) -> dropped
  * tables                            -> flattened to "Header: cell" bullets
  * raw HTML tags and comments        -> dropped
  * horizontal rules, stray whitespace, runs of blank lines

YAML front matter is preserved (``build_epub.py`` reads the title from it),
as is the Markdown heading structure.

Indented code blocks are deliberately *not* stripped: Microsoft Learn units
render code as fenced blocks, while four-space indentation is what the
knowledge-check answer lists use.

``--report DIR`` additionally writes the *sites* where stripping left the
surrounding prose dangling -- "the following code example shows" with no
example after it -- as batched JSON for the polish pass to repair. Point it at
``content/<course>-polish/sites``, where the rest of that pass keeps its
artifacts: they are gitignored there, and they survive this script rewriting
the ``-audio`` tree. See ``polish_ledger.py`` and
``.claude/skills/polish-audio/``.

Examples:
    python strip_for_audio.py content/az-104
    python strip_for_audio.py content/az-104 -o audio/az-104
    python strip_for_audio.py content/az-104 --in-place --images alt
    python strip_for_audio.py content/az-104 --report content/az-104-polish/sites
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections import Counter
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

FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
LEADING_WS_RE = re.compile(r"[ \t]*")
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s]*)>?(?:\s+\"[^\"]*\")?\s*\)")
LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*<?([^)\s]*)>?(?:\s+\"[^\"]*\")?\s*\)")
REF_LINK_DEF_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s+\S+.*$")
VIDEO_RE = re.compile(r"\[Video(?::[^\]]*)?\]")
AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")
BARE_URL_RE = re.compile(r"(?<![(\w])\b(?:https?://|www\.)\S+")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*?)?/?>")
HTML_IMG_RE = re.compile(r"<img\b", re.IGNORECASE)
INLINE_CODE_RE = re.compile(r"(`+)(\s*)(.+?)(\s*)\1", re.DOTALL)
HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
EMPTY_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s*$")
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
# Learn renders a tabbed section as a list of links to in-page #tabpanel_
# anchors. Every in-page anchor link in the tree is one of these, so the rule
# is unambiguous.
TABPANEL_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+\[[^\]]*\]\(\s*#tabpanel_[^)\s]*\s*\)\s*$")
# "tab", not "table": flattened tables are full of the word and are out of
# scope for the polish pass by decision.
TAB_WORD_RE = re.compile(r"\btabs?\b", re.IGNORECASE)

# What makes a removal a *site*: prose that introduced the thing that is now
# gone. Half of all removed code blocks follow a line ending in a colon; the
# rest announce themselves with one of these words. A false positive costs the
# polish worker one glance; a missed broken sentence is read aloud forever.
SITE_PHRASES = ("following", "shown", "screenshot", "diagram", "below",
                "above", "example")
SITE_LINE_CAP = 40  # lines of removed text carried in a site


def split_front_matter(text: str):
    """Return ``(front_matter_or_None, body)``."""
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end == -1:
        return None, text
    line_end = text.find("\n", end + 1)
    if line_end == -1:
        line_end = len(text)
    return text[:line_end + 1], text[line_end + 1:]


def split_cells(row: str) -> "list[str]":
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


def flatten_table(block: "list[str]", stats: Counter) -> "list[str]":
    """Turn a pipe table into bullets that read naturally aloud."""
    rows = [r for r in block if not TABLE_SEP_RE.match(r)]
    if not rows:
        return []
    stats["tables_flattened"] += 1
    header = split_cells(rows[0])
    has_header = len(rows) > 1 and any(TABLE_SEP_RE.match(r) for r in block)
    body = rows[1:] if has_header else rows
    out: "list[str]" = []
    for row in body:
        cells = split_cells(row)
        if not any(cells):
            continue
        if has_header:
            pairs = ["%s: %s" % (h, c) if h else c
                     for h, c in zip(header, cells) if c]
            if len(cells) > len(header):
                pairs += [c for c in cells[len(header):] if c]
        else:
            pairs = [c for c in cells if c]
        if pairs:
            out.append("- " + "; ".join(pairs))
    if out and has_header:
        out.insert(0, "")
    return out


def strip_code_blocks(lines: "list[str]", placeholder: str, stats: Counter,
                      removals=None) -> "list[str]":
    """Drop fenced code blocks, keeping everything else untouched.

    When *removals* is a list, each dropped block is appended to it as
    ``(index, text)``: the index is where the block sat in the returned list,
    the text is the block verbatim, fences included. ``--report`` needs the
    real text -- a worker cannot rewrite "use CREATE TABLE AS CLONE OF" from
    a description like "sql block, 4 lines".
    """
    out: "list[str]" = []
    fence = None  # the opening marker, e.g. "```"
    block: "list[str]" = []
    start = 0
    for line in lines:
        match = FENCE_RE.match(line)
        if fence is None:
            if match and match.group(3).strip().count("`") == 0:
                fence = match.group(2)[0] * len(match.group(2))
                stats["code_blocks_removed"] += 1
                block = [line]
                start = len(out)
                if placeholder:
                    out.append(placeholder)
                continue
            out.append(line)
        else:
            block.append(line)
            if match and match.group(2).startswith(fence[0]) and len(match.group(2)) >= len(fence):
                fence = None
                if removals is not None:
                    removals.append((start, "\n".join(block)))
            continue
    if fence is not None:
        stats["unterminated_code_fences"] += 1
        if removals is not None:
            removals.append((start, "\n".join(block)))
    return out


def clean_inline(text: str, args, stats: Counter) -> str:
    """Apply the inline (within-line) rewrites to one line of Markdown."""
    text = HTML_COMMENT_RE.sub("", text)

    def on_image(match):
        stats["images_removed"] += 1
        alt = match.group(1).strip()
        return alt if (args.images == "alt" and alt) else ""

    text = IMAGE_RE.sub(on_image, text)

    def on_video(match):
        stats["videos_removed"] += 1
        return ""

    text = VIDEO_RE.sub(on_video, text)

    def on_link(match):
        stats["links_flattened"] += 1
        return match.group(1)

    text = LINK_RE.sub(on_link, text)
    text = AUTOLINK_RE.sub("", text)
    text = BARE_URL_RE.sub("", text)

    if args.inline_code == "text":
        text = INLINE_CODE_RE.sub(lambda m: m.group(3), text)
    elif args.inline_code == "drop":
        text = INLINE_CODE_RE.sub("", text)

    text = HTML_TAG_RE.sub("", text)
    text = text.replace("\u00a0", " ")
    text = ZERO_WIDTH_RE.sub("", text)
    # Collapse runs of whitespace within the line only. The leading indent
    # carries list nesting -- including the four-space knowledge-check answer
    # indent -- and Markdown stops seeing a sublist once it is squashed to a
    # single space, so hold it back and put it in front again at the end.
    indent = LEADING_WS_RE.match(text).group(0)
    text = text[len(indent):]
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Close up " ." left behind by a removed inline element, but only when the
    # mark really ends a sentence -- ".NET" must keep the space in front of it.
    text = re.sub(r"[ \t]+([,.;:!?])(?=[ \t]|$)", r"\1", text)
    return (indent + text).rstrip()


def collapse_blanks(lines: "list[str]") -> "tuple[list[str], list[int]]":
    """Squash runs of blank lines to one and trim both ends.

    Returns the lines and a map from each old index (plus a one-past-the-end
    entry) to the index it landed on, which is how ``--report`` turns "the
    code block used to be here" into a line number in the file it writes.
    """
    out: "list[str]" = []
    index_map = [0] * (len(lines) + 1)
    for i, line in enumerate(lines):
        index_map[i] = len(out)
        if line.strip():
            out.append(line)
        elif out and out[-1].strip():
            out.append("")
    while out and not out[-1].strip():
        out.pop()
    index_map[len(lines)] = len(out)
    return out, [min(k, len(out)) for k in index_map]


def cap_lines(text: str, limit: int = SITE_LINE_CAP) -> str:
    lines = text.split("\n")
    if len(lines) <= limit:
        return text
    return "\n".join(lines[:limit] + ["[... %d more lines]" % (len(lines) - limit)])


def dangling(context: str) -> bool:
    """Did *context* promise the thing that was just removed?"""
    context = context.strip()
    if context.endswith(":"):
        return True
    lowered = context.lower()
    return any(phrase in lowered for phrase in SITE_PHRASES)


def removal_class(raw: str) -> str:
    """Site class for a line that vanished entirely."""
    if IMAGE_RE.search(raw) or HTML_IMG_RE.search(raw):
        return "dangling-image-ref"
    if VIDEO_RE.search(raw):
        return "dangling-video-ref"
    return ""  # a bare URL or a stray tag: nothing was promised, nothing broke


def collect_sites(final, index_map, line_map, removals, dropped, tab_links,
                  table_rows) -> "list[dict]":
    """Turn one unit's removals into sites, in line order.

    ``line`` is the body line the removed content used to sit in front of;
    ``process_file`` shifts it past the front matter. Sites inside a flattened
    table are never emitted -- repetitive table bullets are verbosity, not
    breakage, and are out of scope by decision.
    """
    table_lines = set(index_map[k] for k in table_rows)
    sites: "list[dict]" = []

    def context_for(line: int):
        """The last non-blank line before *line*, as the listener hears it."""
        for j in range(min(line, len(final)) - 1, -1, -1):
            if final[j].strip():
                return j, final[j].strip()
        return -1, ""

    candidates = [(index_map[line_map[index]], "dangling-code-ref", text)
                  for index, text in removals]
    candidates += [(index_map[index], removal_class(raw), raw)
                   for index, raw in dropped]
    for line, kind, text in candidates:
        if not kind:
            continue
        context_line, context = context_for(line)
        if not context or context_line in table_lines:
            continue
        if not dangling(context):
            continue
        sites.append({"line": line, "class": kind,
                      "removed": cap_lines(text), "context": context})

    # Tab strips: the bullets are gone, but sentences telling the listener to
    # "select the Recommendation tab" are still there, pointing at nothing.
    if tab_links:
        removed = cap_lines("\n".join(tab_links))
        for line, text in enumerate(final):
            if line in table_lines or not TAB_WORD_RE.search(text):
                continue
            sites.append({"line": line, "class": "tab-ui-ref",
                          "removed": removed, "context": text.strip()})

    sites.sort(key=lambda site: site["line"])
    return sites


def clean_body(body: str, args, stats: Counter, sites=None) -> str:
    lines = body.splitlines()
    removals = [] if sites is not None else None
    lines = strip_code_blocks(lines, args.code_placeholder, stats, removals)

    out: "list[str]" = []
    line_map = [0] * (len(lines) + 1)      # index in lines -> index in out
    table_rows: "list[int]" = []           # out indexes holding table bullets
    dropped: "list[tuple[int, str]]" = []  # (out index, raw line) for --report
    tab_links: "list[str]" = []
    i = 0
    while i < len(lines):
        line_map[i] = len(out)
        line = lines[i]

        # Table block: a pipe row followed by a separator row.
        if (line.lstrip().startswith("|") and i + 1 < len(lines)
                and TABLE_SEP_RE.match(lines[i + 1])):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                line_map[i] = len(out)
                block.append(lines[i])
                i += 1
            # Table rows are the only lines that skip the clean_inline call
            # below, so they get the inline rewrites here -- otherwise links,
            # images and bare URLs survive inside cells. clean_inline leaves
            # "|", "-" and ":" alone, so separator rows still match
            # TABLE_SEP_RE afterwards.
            if args.tables == "flatten":
                block = [clean_inline(row, args, stats) for row in block]
                bullets = flatten_table(block, stats)
                table_rows.extend(range(len(out), len(out) + len(bullets)))
                out.extend(bullets)
            elif args.tables == "keep":
                out.extend(clean_inline(row, args, stats) for row in block)
            else:
                stats["tables_dropped"] += 1
            continue

        if HR_RE.match(line):
            i += 1
            continue
        if REF_LINK_DEF_RE.match(line):
            i += 1
            continue
        # Tab links have to be caught here, on the raw line: by the time
        # clean_inline is done the link is bare text and the bullet reads as a
        # non-sequitur ("- Scenario", "- Recommendation") indistinguishable
        # from a real list item.
        if TABPANEL_ITEM_RE.match(line):
            stats["tab_links_removed"] += 1
            tab_links.append(line.strip())
            i += 1
            continue

        cleaned = clean_inline(line, args, stats)

        # A line that held nothing but an image, a video or a link is now
        # blank (or a naked list bullet) and should go entirely.
        if line.strip() and not cleaned.strip():
            dropped.append((len(out), line))
            i += 1
            continue
        if EMPTY_BULLET_RE.match(cleaned) or cleaned.strip() in (">", "> "):
            dropped.append((len(out), line))
            i += 1
            continue

        out.append(cleaned)
        i += 1
    line_map[len(lines)] = len(out)

    final, index_map = collapse_blanks(out)
    if sites is not None:
        sites.extend(collect_sites(final, index_map, line_map, removals,
                                   dropped, tab_links, table_rows))
    return "\n".join(final).strip() + "\n"


def process_file(src: Path, dst: Path, args, stats: Counter,
                 report: bool = False) -> "list[dict]":
    raw = src.read_text(encoding="utf-8")
    front, body = split_front_matter(raw)
    sites: "list[dict]" = []
    cleaned = clean_body(body, args, stats, sites if report else None)
    offset = 0
    if args.keep_front_matter and front:
        cleaned = front + "\n" + cleaned.lstrip("\n")
        # The front matter's own lines, plus the blank line put after it.
        # Nothing guards front matter from becoming a site because nothing has
        # to: clean_body only ever sees the body.
        offset = front.count("\n") + 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(cleaned, encoding="utf-8")
    stats["files"] += 1
    for site in sites:
        site["line"] += offset + 1  # 1-based, as an editor counts
    return sites


def write_report(sites: "list[dict]", report_dir: Path, batch_size: int) -> int:
    """Write sites to ``<dir>/batch-NN.json``, whole units at a time.

    Batching by unit rather than by site is what lets the polish orchestrator
    hand each worker a filename and never hold unit text itself.
    """
    root = long_path(report_dir)
    root.mkdir(parents=True, exist_ok=True)
    for stale in sorted(root.glob("batch-*.json")):
        stale.unlink()  # a re-run must not leave the last run's batches behind

    batches: "list[list[dict]]" = []
    current: "list[dict]" = []
    units = 0
    previous = None
    for site in sites:
        if site["file"] != previous:
            if units >= batch_size:
                batches.append(current)
                current, units = [], 0
            previous = site["file"]
            units += 1
        current.append(site)
    if current:
        batches.append(current)

    for number, batch in enumerate(batches, start=1):
        with open(root / ("batch-%02d.json" % number), "w", encoding="utf-8") as handle:
            json.dump(batch, handle, indent=1, ensure_ascii=False)
            handle.write("\n")
    return len(batches)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Strip Markdown of content that is not audio-friendly.")
    parser.add_argument("src", type=Path, help="Directory of .md files (searched recursively)")
    parser.add_argument("-o", "--out", type=Path,
                        help="Output directory (default: <src>-audio)")
    parser.add_argument("--in-place", action="store_true",
                        help="Rewrite the files in SRC instead of mirroring them")
    parser.add_argument("--images", choices=("drop", "alt"), default="drop",
                        help="drop images entirely, or keep their alt text (default: drop)")
    parser.add_argument("--tables", choices=("flatten", "drop", "keep"), default="flatten",
                        help="flatten tables into bullets, drop them, or leave them "
                             "(default: flatten)")
    parser.add_argument("--inline-code", choices=("text", "drop"), default="text",
                        help="keep inline code as plain words or drop it (default: text)")
    parser.add_argument("--code-placeholder", default="",
                        help="Text to leave in place of each removed code block "
                             "(default: leave nothing)")
    parser.add_argument("--no-front-matter", dest="keep_front_matter", action="store_false",
                        help="Drop YAML front matter (build_epub.py reads titles from it)")
    parser.add_argument("--report", type=Path, metavar="DIR",
                        help="Write the dangling-reference sites that stripping left "
                             "behind as batch-NN.json in DIR (replacing any already there)")
    parser.add_argument("--batch-size", type=int, default=8, metavar="N",
                        help="Units per --report batch file (default: 8)")
    args = parser.parse_args()

    src = args.src.resolve()
    if not src.is_dir():
        print("Not a directory: %s" % src, file=sys.stderr)
        return 2

    if args.in_place:
        out = src
    else:
        out = (args.out or src.parent / (src.name + "-audio")).resolve()
        if out == src:
            print("Output directory equals the source; use --in-place instead.",
                  file=sys.stderr)
            return 2

    scan_root, write_root = long_path(src), long_path(out)
    files = sorted(p for p in scan_root.rglob("*.md") if p.is_file())
    if not files:
        print("No .md files found under %s" % src, file=sys.stderr)
        return 2

    stats: Counter = Counter()
    sites: "list[dict]" = []
    for path in files:
        relative = str(path.relative_to(scan_root)).replace(os.sep, "/")
        try:
            found = process_file(path, write_root / path.relative_to(scan_root),
                                 args, stats, bool(args.report))
        except Exception as exc:  # noqa: BLE001 - one bad unit must not stop the run
            stats["failed"] += 1
            print("  ! failed %s: %s" % (relative, exc), file=sys.stderr)
            continue
        # The file comes first in each site so that a batch reads unit by unit.
        sites.extend({"file": relative, "line": site["line"], "class": site["class"],
                      "removed": site["removed"], "context": site["context"]}
                     for site in found)

    # Carry the manifest across so build_epub.py can still use it.
    if not args.in_place:
        for extra in scan_root.rglob("manifest.json"):
            dest = write_root / extra.relative_to(scan_root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(extra, dest)

    print("Cleaned %d file(s) -> %s" % (stats["files"], out))
    for key in ("code_blocks_removed", "images_removed", "videos_removed",
                "links_flattened", "tab_links_removed", "tables_flattened",
                "tables_dropped", "unterminated_code_fences", "failed"):
        if stats[key]:
            print("  %-24s %d" % (key.replace("_", " ") + ":", stats[key]))

    if args.report:
        batches = write_report(sites, args.report, max(1, args.batch_size))
        units = len(set(site["file"] for site in sites))
        print("Reported %d site(s) in %d unit(s) -> %s (%d batch file(s))"
              % (len(sites), units, args.report, batches))
        for key in ("dangling-code-ref", "dangling-image-ref",
                    "dangling-video-ref", "tab-ui-ref"):
            count = sum(1 for site in sites if site["class"] == key)
            if count:
                print("  %-24s %d" % (key + ":", count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
