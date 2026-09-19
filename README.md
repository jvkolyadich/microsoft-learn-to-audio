# microsoft-learn-to-audio

Turn a Microsoft Learn course into an EPUB you can listen to with Speechify or
any other text-to-speech app.

## Setup

```
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux/macOS
pip install -r requirements.txt
```

Keep the virtualenv active for the commands below.

## 1. Download

```
python src/download_course.py https://learn.microsoft.com/en-us/training/courses/dp-600t00/
```

Writes the course as Markdown to `content/<course-name>/`, one file per unit,
numbered to keep the reading order:

```
content/dp-600t00/
  manifest.json
  01-explore-analytics-data-stores/                 <- learning path
    01-introduction-end-analytics-use-microsoft/    <- module
      01-introduction.md                            <- unit
      02-explore-analytics-fabric.md
```

A learning path or module URL works in place of a course URL. To download a
course a piece at a time into the same tree, pass the course URL with
`--learning-path <url>` or `--module <url>`.

Other flags: `--list` (show the unit tree and stop), `--force` (re-download
existing files), `-o`, `--workers`, `--delay`, `--locale`, `--max-name`,
`--refresh-catalog`.

## 2. Strip for audio

```
python src/strip_for_audio.py content/dp-600t00
```

Writes a cleaned copy to `content/dp-600t00-audio/` with code blocks, images,
links, tables and other things that don't read well aloud removed or flattened.

Flags: `-o`, `--in-place`, `--images {drop,alt}`, `--tables
{flatten,drop,keep}`, `--inline-code {text,drop}`, `--code-placeholder TEXT`,
`--no-front-matter`, `--report DIR`, `--batch-size N`.

## 2.5 Polish the text (optional)

Stripping removes code and images but not the sentences that introduced them
("the following code example shows..."). In Claude Code, run:

```
/polish-audio content/dp-600t00-audio
```

This finds those sentences, rewrites or removes them in place, and saves the
edits to `content/dp-600t00-polish/` in case they need to be re-applied later.

To do this without Claude Code, `strip_for_audio.py --report DIR` writes the
broken sentences as JSON batches for any tool to fix, and
`polish_ledger.py record` / `replay` save and reapply the edits. See
`--help` on each.

## 3. Build the EPUB

```
python src/build_epub.py content/dp-600t00-audio -o dp-600.epub
```

Makes an EPUB 3 with a learning path / module / unit table of contents.

Flags: `--title`, `--author`, `--language`, `--keep-images`.
