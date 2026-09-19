# AGENTS.md

Guidance for coding agents working in this repository. `README.md` covers how
to *use* the scripts; this file covers how to *change* them.

## Environment

A virtualenv lives in `.venv/`. Always use it — the scripts are not installed
on the system interpreter in a way you should rely on.

```
.venv/Scripts/python.exe src/download_course.py --help     # Windows
.venv/bin/python src/download_course.py --help             # Linux/macOS
```

Dependencies are pinned loosely in `requirements.txt`: `requests`,
`beautifulsoup4`, `markdownify`, `Markdown`, `EbookLib`. Add a dependency only
if it earns its place; the scripts are otherwise stdlib-only.

## Layout

```
src/download_course.py   Learn -> content/<course>/<path>/<module>/<unit>.md
src/strip_for_audio.py   that tree -> <tree>-audio, minus non-audio content
src/polish_ledger.py     records the LLM polish pass as replayable edits
src/build_epub.py        a tree -> one .epub with a 3-level TOC
content/<course>-polish/ sites, ledger and misses for one course; gitignored
.claude/skills/polish-audio/      drives the polish pass from Claude Code
.claude/agents/audio-polisher.md  its worker, limited to Read and Edit
.cache/                  catalog API cache (project root, not src/)
content/                 default download target; gitignored
```

Four standalone scripts that form a pipeline. **They deliberately do not
import each other** — each one must run on its own, so a small amount of
duplication (`slugify`, `long_path`, front-matter parsing) is intentional. Do
not "fix" this by extracting a shared module unless you are asked to. They are
run as scripts by path, never imported as a package; `src/` has no
`__init__.py` and needs none.

`download_course.py` locates the catalog cache as
`Path(__file__).resolve().parent.parent / ".cache"` — the project root. If the
scripts ever move again, that path moves with them.

The contracts between them:

- **YAML front matter** on every unit file (`title`, `uid`, `url`, `order`,
  `duration_minutes`, `module`, `learning_path`, `course`, plus `*_uid`).
  Script 2 preserves it; script 3 reads `title`, and falls back to
  `learning_path`/`module` when there is no manifest.
- **`manifest.json`** at the tree root, written by script 1 and copied through
  by script 2. It is the authoritative ordering for script 3. Script 3 has a
  directory-walking fallback for when it is absent — keep both paths working.
- **Sites**, the JSON that `strip_for_audio.py --report` writes and the polish
  workers read: `{file, line, class, removed, context}`, batched by unit into
  `batch-NN.json`. `polish_ledger.py replay` writes its misses in the same
  shape, so they can go straight back to a worker. Changing that shape means
  changing both scripts and the worker prompt in `.claude/agents/`.
- **The ledger**, the `{file, line, find, replace}` entries
  `polish_ledger.py record` derives by diffing a polished tree against a fresh
  one. `find` must match exactly once — the rule the Edit tool enforces, and
  the reason `record` widens context until it is unique.

**Polish artifacts never go in git, and never go in `.cache/`.** The ledger
and the site batches quote Learn prose verbatim, which is the same reason
`content/` is ignored, so they default to `content/<course>-polish/` and
`.gitignore` also catches them by name at the project root. `.cache/` would be
worse than the repo root: it holds the refetchable catalog, so anyone clearing
it would delete the one record of an LLM pass that cannot be reproduced. Nor
do they belong inside the `-audio` tree, which step 2 rewrites from scratch and
which people delete to force that.

## Things that will bite you

**Unit URLs cannot be derived from catalog UIDs.** This looks like an obvious
optimization and it is wrong. Measured over 96 units in 12 random modules, 45
of the derived URLs 404: modules number and name their unit pages
inconsistently with their unit UIDs (`1-1-create` vs `1-create`, doubled dots,
gaps in numbering). Unit URLs come from each module page's `#unit-list`, which
is authoritative. The UID-derivation code in `fetch_units` is a last-resort
fallback only, and it warns when it fires.

**Course and learning-path pages render their children in JavaScript.** Their
HTML contains no links to child modules or paths, so scraping them is not an
option. The hierarchy comes from the public catalog API
(`learn.microsoft.com/api/catalog/?locale=en-us`), cached in `.cache/` for
seven days. The response is ~14 MB — do not refetch it casually while
iterating; `--refresh-catalog` forces it when you actually need to.

**Names come from URL segments, not titles.** Every level of the tree is named
from the last segment of its Learn URL, which is far shorter than a slugified
title and is what keeps a four-level tree inside the Windows path limit
(DP-600: 202 characters, down from 242). The leading number on a unit segment
is *not* its position — one DP-600 module numbers its ten unit pages 1, 2, 3,
3a, 4, 4b, 4c, 5, 6, 7 — so `unit_name` replaces it with the zero-padded
ordinal. Naming schemes are not backward compatible: `merge_manifest` merges
partial downloads by path, so a tree written by an older scheme has to be
re-downloaded, not merged into.

**The polish orchestrator must never read unit text.** The skill holds batch
filenames and one-line worker reports, nothing else; the workers hold the
text. 168 units do not fit in one context, and an orchestrator that starts
reading them degrades every batch after it compacts. Same reasoning puts the
ledger on a diff rather than on what the workers say they did.

**The polish worker is restricted to `Read` and `Edit` on purpose.** It cannot
run commands or create files, and anchored find/replace fails loudly instead
of guessing. Whole-file rewriting — where a paraphrased paragraph or a dropped
bullet corrupts a unit invisibly — is structurally impossible. Do not widen
those tools to "make it easier".

**Front matter is a contract.** `--report` never emits a site inside it,
`polish_ledger.py record` refuses to ledger a change there and warns, and the
worker prompt states the rule as well. Keep all three.

**Windows caps paths at 260 characters.** A four-level course tree under a
normal output directory hits that limit. Every filesystem call in all four
scripts goes through `long_path()`, which applies the `\\?\` prefix. Do not
replace those with plain `Path.write_text` / `Path.mkdir` / `Path.exists` —
that regression is silent until someone downloads a course with long titles.
The polish worker's Read and Edit are the one part of the pipeline with no
`long_path()` protection, which is why names had to get shorter before the
polish pass could exist. Names are still capped at 40 characters
(`MAX_NAME`, `--max-name`); URL segments top out around 42, so the cap
rarely fires and raising it to 60 is a reasonable open question.

**Do not strip four-space-indented blocks in `strip_for_audio.py`.** Learn
renders code as fenced blocks; four-space indentation is what the
knowledge-check answer lists use. Stripping it would delete quiz answers.

**Regexes that touch prose need a following-context guard.** A cleanup rule of
`\s+([,.;:!?])` looked fine and silently turned "runs anywhere .NET" into
"runs anywhere.NET". The current rule requires whitespace or end-of-line after
the punctuation mark. Apply the same care to any new prose rewrite.

## Conventions

- `%`-formatting throughout, not f-strings. Keep it consistent.
- `argparse` CLIs with a module docstring ending in an `Examples:` block.
- Comments explain *why*, especially where the code looks needlessly
  defensive — most of that defensiveness is load-bearing (see above).
- Failures on a single unit are logged to stderr and counted, never fatal: a
  52-unit download must not die on one bad page.

## Testing

There is no test suite. Verify by running the pipeline against real content.
Cheap checks first:

```
# no network writes, ~2 requests: confirms catalog resolution and unit listing
.venv/Scripts/python.exe src/download_course.py <url> --list

# a small real module, 7 units, end to end (content/ is gitignored)
.venv/Scripts/python.exe src/download_course.py https://learn.microsoft.com/en-us/training/modules/shape-data/
.venv/Scripts/python.exe src/strip_for_audio.py content/shape-data
.venv/Scripts/python.exe src/build_epub.py content/shape-data-audio
```

Re-running a download skips files that already exist; pass `--force` when you
have changed the HTML-to-Markdown conversion and need them rewritten.

The polish pass itself runs from Claude Code as `/polish-audio <tree>-audio`;
everything except the workers is deterministic, so it costs nothing to check:

```
# does --report find the known sites, and only those? no model, no network
.venv/Scripts/python.exe src/strip_for_audio.py content/dp-600t00 --report content/dp-600t00-polish/sites

# after a polish run: the diff should be small and every hunk explicable
.venv/Scripts/python.exe src/strip_for_audio.py content/dp-600t00 -o content/dp-600t00-fresh
git diff --no-index content/dp-600t00-fresh content/dp-600t00-audio

# and the ledger must round-trip: applied == entries, missed == 0, trees equal
.venv/Scripts/python.exe src/polish_ledger.py record content/dp-600t00-audio content/dp-600t00-fresh
.venv/Scripts/python.exe src/polish_ledger.py replay content/dp-600t00-polish/polish-edits.json content/dp-600t00-fresh
```

Known-good targets in DP-600: the CREATE TABLE AS CLONE OF example in
`04-get-started-data-warehouse/03-understand-data-warehouse-fabric.md` must be
flagged with its code in `removed`; the tab strips in
`01-choose-data-store-fabric/06-exercise.md` must leave no bare `- Scenario` /
`- Recommendation` bullets and must flag the "select the **Recommendation**
tab" sentence; units where stripping removed nothing must appear in no batch;
and the Tip ending "see the Clone table in Microsoft Fabric documentation"
must not be flagged — it reads fine.

Useful coverage when changing the converters:

- `dp-600t00` (course) — 168 units, what the polish pass was measured on
- `shape-data` — tables, quiz/knowledge check
- `host-a-web-app-with-azure-app-service` — images, notes, tables, code
- `describe-cloud-compute` — an embedded video
- `gh-200t00` (course) — the full three-level hierarchy, 52 units

After building an EPUB, check it is a sound zip and that every XHTML part is
well-formed XML (`zipfile.testzip()` plus `ElementTree.fromstring` over each
`.xhtml`/`.opf`/`.ncx`). A malformed chapter will not surface until a reader
rejects the file.

## Network manners

The scripts hit a live Microsoft site. Defaults are 4 workers and a 0.25 s
throttle shared across threads, with retry and backoff on 429/5xx. Do not
raise the defaults, and do not remove the throttle to make a test run faster.
