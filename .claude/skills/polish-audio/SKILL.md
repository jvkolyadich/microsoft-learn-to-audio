---
name: polish-audio
description: Repair the dangling sentences that strip_for_audio.py leaves behind in an -audio tree ("the following code example shows" with no example after it), by fanning batches of sites out to audio-polisher subagents, then record the result as a replayable ledger. Use when asked to polish, repair or clean up an -audio tree, or after changing the stripper and re-running step 2.
---

# Polish an `-audio` tree

Usage: `/polish-audio content/<course>-audio`

The source tree is that path without the `-audio` suffix. Use
`.venv/Scripts/python.exe` on Windows, `.venv/bin/python` elsewhere.

Everything this pass produces -- the site batches, the ledger, any misses --
belongs in `content/<course>-polish/`, beside the tree and never inside it:
step 1 rewrites the `-audio` tree from scratch, and people delete that tree to
force it. `content/` is gitignored, which these artifacts need to be: they
quote Learn prose verbatim.

## The one rule that matters

**Never read unit text yourself.** Not the units, not the batch files, not
`polish-edits.json`. You hold batch *filenames*, command output and one-line
worker reports -- nothing else. A course is 168 units; an orchestrator that
reads any of them hits compaction somewhere in the middle and the quality of
every batch after that point drops. The workers read the text. You do not.

## Steps

1. **Re-strip and report.** This regenerates the audio tree and the batches
   together, so the sites always match the text on disk:

   ```
   .venv/Scripts/python.exe src/strip_for_audio.py content/<course> --report content/<course>-polish/sites
   ```

   The summary line says how many sites in how many units. If it says zero,
   stop and say so.

2. **List the batches**: `ls content/<course>-polish/sites/batch-*.json`.

3. **Spot-check with one worker first.** Spawn a single `audio-polisher`
   subagent for `batch-01.json`, then check what it did before releasing the
   rest. Read the *diff*, not the units -- and get the unit list without
   opening the batch file:

   ```
   .venv/Scripts/python.exe -c "import json,sys;print('\n'.join(sorted(set(s['file'] for s in json.load(open(sys.argv[1],encoding='utf-8'))))))" content/<course>-polish/sites/batch-01.json
   ```

   Then `git diff --no-index` each of those units against a freshly stripped
   copy (step 5 makes one; make it early if you want the check). Every hunk
   should be explicable from a site, and none should touch front matter, a
   heading or a table bullet. If the worker overreached, fix
   `.claude/agents/audio-polisher.md` and redo this batch before going on.

4. **Release the rest.** Spawn one `audio-polisher` per remaining batch file,
   several at a time. Each prompt carries only the tree root and the batch
   path:

   > Polish the sites in `content/<course>-polish/sites/batch-03.json`.
   > The tree root is `content/<course>-audio`. Follow your instructions
   > exactly and reply with the one-line report only.

   Workers edit different units, so batches are safe to run in parallel.
   Collect the one-line reports and add them up.

5. **Record the ledger.** `content/` is gitignored and step 2 rewrites the
   audio tree from scratch, so without this the whole pass is lost the next
   time the stripper changes:

   ```
   .venv/Scripts/python.exe src/strip_for_audio.py content/<course> -o content/<course>-fresh
   .venv/Scripts/python.exe src/polish_ledger.py record content/<course>-audio content/<course>-fresh
   ```

   With no `-o`, the ledger lands in
   `content/<course>-polish/polish-edits.json`.

   `record` warns if anything changed inside front matter. Investigate that
   rather than shrugging at it: `build_epub.py` reads the title from there.

6. **Report**: units touched, sites fixed, sites skipped, edits recorded. Say
   where the ledger is and that `content/<course>-fresh` can be deleted once
   the diff has been looked at.

## After the stripper changes

Do not re-polish from scratch. Re-strip with `--report`, replay the ledger,
and polish only what the replay could not place plus whatever sites are new:

```
.venv/Scripts/python.exe src/strip_for_audio.py content/<course> --report content/<course>-polish/sites
.venv/Scripts/python.exe src/polish_ledger.py replay content/<course>-polish/polish-edits.json content/<course>-audio
```

`replay` never guesses: an edit whose anchor text moved or multiplied is a
*miss*, written to `content/<course>-polish/misses/` as batch files in exactly
the site format the workers already read. Hand those batches to
`audio-polisher` the same way, then record the ledger again.
