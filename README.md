# microsoft-learn-to-audio

Turn a Microsoft Learn course into an EPUB you can listen to with Speechify or
any other text-to-speech app.

Three scripts, run in order:

| Script | What it does |
| --- | --- |
| `src/download_course.py` | Downloads every unit of a course as Markdown |
| `src/strip_for_audio.py` | Removes images, code, links and other things that don't read well aloud |
| `src/build_epub.py` | Joins the Markdown files into one EPUB with a table of contents |

There is also an optional step between 2 and 3. It uses an LLM to fix sentences
that now point at nothing, like "the following code example shows..." with no
example after it. See [step 2.5](#25-polish-the-text-optional).

## Setup

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt     # Windows
.venv/bin/python -m pip install -r requirements.txt             # Linux/macOS
```

Activate the virtualenv (`.venv\Scripts\activate`) before running the commands
below.

## 1. Download

```
python src/download_course.py https://learn.microsoft.com/en-us/training/courses/dp-600t00/
```

This writes to `content/<course-name>/`:

```
content/dp-600t00/
  manifest.json
  01-explore-analytics-data-stores/                 <- learning path
    01-introduction-end-analytics-use-microsoft/    <- module
      01-introduction.md                            <- unit
      02-explore-analytics-fabric.md
      ...
```

Folders and files are named after the last part of their Learn URL, with a
number in front to keep them in reading order. URL names are short, which keeps
the paths under the Windows 260-character limit.

Each file starts with front matter: the unit title, source URL, duration and
where it sits in the course. `manifest.json` stores the full course structure
and sets the order in the EPUB.

### Downloading part of a course

You can pass a learning path or module URL instead of a course URL:

```
python src/download_course.py https://learn.microsoft.com/en-us/training/paths/github-actions-2/
python src/download_course.py https://learn.microsoft.com/en-us/training/modules/github-script/
```

Or download a course but keep only one part of it. This keeps the full folder
layout, so you can download a course a piece at a time into one tree:

```
python src/download_course.py <course-url> --learning-path <path-url-or-uid>
python src/download_course.py <course-url> --module <module-url-or-uid>
```

Targets can be a URL, a catalog uid like `learn.github.github-actions-ci`, or a
plain slug. If you pass a wrong one, the script lists the valid options.

Other flags: `--list` (show the unit tree and stop), `--force` (re-download
files that already exist), `--workers`, `--delay`, `--locale`, `--max-name`,
`--refresh-catalog`.

## 2. Strip for audio

```
python src/strip_for_audio.py content/dp-600t00
```

This writes a cleaned copy next to the source, with `-audio` added to the name.
By default it:

- removes code blocks, images and `[Video: ...]` markers
- removes tab strips (they turn into meaningless bullets otherwise)
- turns `[text](url)` into `text` and removes bare URLs
- turns inline `` `code` `` into plain words
- turns tables into `- Header: cell; Header: cell` bullets
- removes raw HTML, horizontal rules and extra blank lines

Front matter and headings are kept. Blocks indented by four spaces are kept
too, because that is how the knowledge-check answer lists are written.

Flags: `-o/--out`, `--in-place`, `--images {drop,alt}`, `--tables
{flatten,drop,keep}`, `--inline-code {text,drop}`, `--code-placeholder TEXT`,
`--report DIR`, `--batch-size N`.

## 2.5 Polish the text (optional)

Stripping removes code and images, but not the sentences that introduced them.
The `--report` flag finds those spots:

```
python src/strip_for_audio.py content/dp-600t00 --report content/dp-600t00-polish/sites
```

This writes `sites/batch-01.json`, `batch-02.json`, and so on. Each entry
has the file, the line, what was removed and the sentence that no longer makes
sense. Only units that need fixing are listed.

In Claude Code, run `/polish-audio content/dp-600t00-audio`. It gives each batch
to a subagent that can only read and edit files. The subagent rewrites or
deletes each broken sentence in place. The batch files are plain JSON, so any
other tool can use them too.

Then save what the pass changed, so you don't lose it if you re-run step 2:

```
python src/strip_for_audio.py content/dp-600t00 -o content/dp-600t00-fresh
python src/polish_ledger.py record content/dp-600t00-audio content/dp-600t00-fresh
```

`record` compares the polished tree with a fresh strip and saves each change as
a `{file, find, replace}` edit. After changing the stripper, re-strip and replay
the edits instead of polishing again:

```
python src/polish_ledger.py replay content/dp-600t00-polish/polish-edits.json content/dp-600t00-audio
```

If an edit's text has moved or appears more than once, the script does not
guess. It writes that edit to `misses/` in the same batch format, so you can
hand it back to the polish pass.

Everything this step produces goes in one folder next to the course:

```
content/dp-600t00-polish/
  polish-edits.json      <- the record of the LLM pass
  sites/batch-01.json    <- what --report found
  misses/batch-01.json   <- what replay could not place
```

It sits next to the tree, not inside it, because step 2 rewrites the `-audio`
tree from scratch. It is also inside `content/`, so git ignores it. That
matters: the edits quote course text word for word. It also means the polish
only exists on your disk. If a course took a while to polish, back that folder
up somewhere outside this repo.

## 3. Build the EPUB

```
python src/build_epub.py content/dp-600t00-audio -o dp-600.epub
```

This makes an EPUB 3 with a three-level table of contents: learning path,
module, unit. Each learning path and module gets a short title page, so the
TTS app announces where you are.

Order comes from `manifest.json` if it exists. Otherwise the file name numbers
and front matter are used.

Flags: `--title`, `--author`, `--language`, `--keep-images`.

Upload the `.epub` file to Speechify.

## How the content is found

Learn's course and learning-path pages load their contents with JavaScript, so
the HTML has no links to follow. The course structure comes from the public
catalog API (`learn.microsoft.com/api/catalog/`) instead, cached for a week
under `.cache/`.

Unit URLs come from each module's own page. They can't be worked out from
catalog uids, because about half of all modules name their unit pages
differently from their unit uids. Folder and file names come from those URLs,
so an oddly named page gets an oddly named file. The content is still correct.
