---
name: report-publish
description: Publish a finished report as a page a named set of people can read and ask questions about. Covers gathering recipients and a cap, building the one archive the reading room expects, the size discipline for a large bundle, and the mint-then-upload-then-share procedure. Use when someone wants to hand a finished report to specific people, not for sharing a notebook or a chart in the thread.
---

# report-publish

You are publishing a finished report as a reading room: a page where the
people you name can read the report and ask an agent questions about it,
grounded in the analysis behind it.

## 1. When to use this

Use this when someone has a finished report and wants specific people to be
able to read it and ask follow-up questions about the numbers in it. This is
not for sharing an interactive notebook or dashboard — that is a different
tool. It is not for posting a chart or a table directly in the conversation
either. It is specifically for a finished, standalone report that a named
audience should be able to sit with and interrogate on their own time.

## 2. What to gather first

Before you build anything, gather:

- **A short slug.** Lowercase letters, numbers and hyphens only — it becomes
  part of every recipient's link.
- **A human title** for the report.
- **One entry per recipient**, each with a name and a label naming who they
  are (an email, a role, a handle — whatever tells the two of you apart
  later).
- **A dollar cap** for the whole report's questions. This cap covers every
  recipient's questions together, not one cap each — plan it accordingly.
- **Which agent should answer questions.** This defaults to the agent already
  configured for the workspace; you may name a different one. Either way, the
  reader gets a variant of that agent with the same voice and knowledge, minus
  every integration it would otherwise have — a reader-facing agent never
  reaches into the workspace's chat tools or its own extra connections.

## 3. Build the archive

Everything the reading room serves comes from ONE gzip archive. The report
PDF sits at its root. The analysis directories sit beside it, at the same
level. Every path inside the archive must be relative — no leading slash, no
parent-directory reference. An absolute path or a relative path that climbs
out of the archive root gets the whole upload refused, so build the archive
from inside the directory you're compressing, not by adding files with their
full path baked in.

The layout, as a tree:

```
report.pdf     the report itself, at the archive root
README.md      how the answering agent should answer, and how to rebuild the report
manifest.json  links each figure to its render script, its table, its model and its source data
data/          the source data as it was loaded, before any joins
prep/          scripts that join and clean data/ into the tables below
models/        the fitted model(s): scripts, summaries, diagnostics, full trace
tables/        the per-figure and headline numbers, already computed
figures/       one render script (and rendered image) per figure
typst/         the report's source — edit and rebuild from here
```

Write a real `README.md` and a real `manifest.json`, not placeholders — these
two files are what make the answers good, because the answering agent reads
them first, before anything else in the bundle. A `manifest.json` a reader
never sees but an answering agent needs is the difference between "why is
this number what it is" getting a sourced answer and getting a guess. Give
the README the answering persona and any report-specific notes; give the
manifest a real chain from each figure to its table, its model and its source
data, plus a run order for the scripts.

## 4. Size discipline

There is a hard cap on the archive's size. If your bundle does not fit under
it, drop things in this order:

1. **Raw posterior draws and trace files** — the biggest single space cost,
   and the least likely to be asked for directly.
2. **Intermediate caches** — anything a script recomputes cheaply and does
   not need to ship pre-built.
3. **Anything regenerable from a script that is already in the archive** — if
   `models/fit.py` produces it and `models/fit.py` is in the bundle, the
   output doesn't also need to be.

Keep the tables, the manifest and the source data no matter what — those are
exactly what a "why is this number what it is" question resolves against,
and dropping them breaks the whole point of the reading room. If the archive
still does not fit after dropping the three categories above, subset the
source data (a sample, a recent window, an aggregate) rather than dropping
the tables or the manifest.

## 5. The procedure

1. Call the publish tool with the slug, title, recipients, cap and agent
   choice from step 2. It returns a one-time upload URL and one link per
   recipient.
2. PUT the archive to the upload URL with a single curl command:
   ```bash
   curl -sS -X PUT --data-binary @bundle.tar.gz "<upload_url>"
   ```
   Never put the archive's contents through a tool argument — a tool call
   truncates large content, and a multi-megabyte archive will not survive the
   round trip. The upload URL is what carries the bytes; the tool call only
   ever carries small values.
3. Relay each person their own link. The links are per-recipient and must
   not be swapped or shared — sending Ada's link to Ben means Ben reads Ada's
   report under Ada's name, and sending one link to everyone collapses the
   isolation the reading room is built around entirely.

## 6. Afterwards

Each recipient gets their own conversation with the answering agent, and the
cap you set covers all of them together, not one each. A recipient can ask
the answering agent to revise the report; the agent rebuilds it and uploads
the new version, which replaces the old one in every recipient's viewer.
Deleting the report revokes every link at once and cannot be undone — a
recipient who needs access again after that would need a fresh publish and a
new link.

## 7. What not to do

- Do not put client data in a repository. The archive replaces that path
  entirely — there is no reason for a client's data to live anywhere else.
- Do not reuse one link for several people. Every recipient gets their own.
- Do not publish without a cap. An uncapped report has no limit on what
  reader questions can spend.
- Do not paste the archive into a tool argument. Mint the upload URL, then
  curl the file to it.
