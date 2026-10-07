---
name: marimo_notebooks
description: Publish interactive marimo notebooks via the daimon MCP server. Mint a one-time upload URL with create_notebook_upload_url, get the .py into a sandbox file, and curl -X PUT --data-binary it to the URL — source never goes through a tool argument, which truncates. ttl_days (1-365, default 1) sets how long a scratch notebook is kept; permanent=True publishes the same notebook as a read-only shareable blog instead. Also covers attaching data files, list_notebooks and delete_notebook. Use when someone asks for a notebook, dashboard, data explorer, or to publish an analysis as a blog post.
---

# marimo_notebooks

Publish an interactive marimo notebook for the user. You mint a one-time upload
URL, get the notebook's `.py` into a sandbox file, and
`curl -X PUT --data-binary` it to the URL — the source never goes through a tool
argument, which truncates large notebooks. The curl response returns a URL
whose `access_token` is the notebook's only key. Share the whole URL, and only
with the user who asked.

A notebook is data work, not decoration. The person on the other end is usually
trying to answer a real question. A polished notebook that answers the *wrong*
question, or that "discovers" conclusions you secretly invented, or that errors
on the first cell, is worse than no notebook — it looks authoritative while
being hollow. The three rules below exist to prevent exactly that. Read them
before you build anything.

## Scratch notebook or permanent blog

One tool publishes both. `create_notebook_upload_url(slug=..., permanent=..., ttl_days=...)`:

| | `permanent=False` (default) | `permanent=True` |
|---|---|---|
| Shape | a read-only app, source hidden (`editable=True`: the code editor) | a read-only app, source hidden |
| Lifetime | deleted after `ttl_days` (default 1, up to 365); the editor lasts one day | kept until you delete it |
| Slug | optional; omit for a random one | choose a meaningful, stable name — it is part of the URL |

**Set `ttl_days` to how long the person needs the link.** One day suits a quick
look. A client review, or a link going into a report or email, needs longer:
ask or pick a sensible span (14, 30, 90) rather than letting it break a day
later. Say when the link expires when you share it (`expires_at` in the curl
response). Re-uploading the same slug restarts its `ttl_days` from now.

Both kinds survive host restarts. The host stops a notebook nobody has opened
for a couple of hours and starts it again on the next visit, so a link that has
been quiet can take a few seconds to load. That is expected, not an error. Only
the full link, with its `access_token`, starts a stopped notebook, so always
share the whole URL from the curl response, never a shortened one.

**Default to `permanent=False`.** Publish the scratch version, let the user look
at it, and re-upload the *same slug* with `permanent=True` once it is worth
keeping. Deciding permanence before anyone has seen the notebook is how hosts
accumulate blogs nobody wanted.

**Pass `editable=True` only when the user asked to edit the notebook.** An editor
link lets anyone who holds it run arbitrary code on the notebook host. Never
pass it for a link going to a client or a shared channel. The tool refuses it
unless the operator has turned editors on; if it does, publish read-only.

Both are a live Python kernel, not a WASM export — so PyMC/ArviZ widgets
genuinely work, sliders re-plot a real posterior, dropdowns switch parameters.
That is what this tool buys over a static image.

A permanent blog runs its cells **on every page load, per reader**. See
"Precompute, never sample live" below — it is the rule that matters most there.

## 1. Align before you build

Building the wrong notebook is expensive: it wastes a round-trip and hands the
user a confident-looking artifact that answers a question they didn't ask. A
one-line check is cheap. So spend it — but only when it changes what you build.
This is a general assistant, not a wizard. Do **not** interrogate the user.

**Your first move on any notebook request is to classify it, not to write
code.** Decide: do I have the data, and is the scope unambiguous?

- **If yes to both → build now.** "Plot column A of the CSV I attached" needs no
  clarification — just build it. Don't manufacture questions.
- **If a trigger below fires → STOP. Your entire reply this turn is the
  clarifying question. Do not write notebook code, do not upload or publish,
  do not "build it anyway just in case."** Ask, then wait for the answer. One
  short message, offering concrete options.

**Triggers — pause for ONE short clarifying message when any is true:**

- **You have no data and would have to invent it.** This is the most important
  trigger and the easiest to rationalize away. "I'll just use realistic
  synthetic data, no external API needed" is **exactly the failure mode** — do
  not do it. Either offer to pull real data from a named public source (you have
  a bash tool with network — see §2), or ask the user to attach theirs, or ask
  whether a clearly-labelled synthetic *demo* is what they want. Let them choose;
  do not choose "fabricate" for them.
- **The scope is ambiguous.** "All races" — every edition of one Grand Prix,
  or every Grand Prix on the calendar? "Last 5 years" of what? State the
  concrete readings and let the user pick. A wrong scope guess can burn an
  entire build on the wrong dataset.
- **There are several defensible ways to model the question.** "What drives
  overtaking?" could mean a physics heuristic, an empirical regression on real
  results, or historical counts. Name 2–3 framings, say which you'd pick and
  why, and let the user steer. This is the single highest-value thing you can
  do — early users consistently call it the difference between "cool demo" and
  "actually useful."

**When you do proceed on an assumption** (because none of the above tripped),
state the consequential interpretation in one line up front — "Building for all
24 Grands Prix of the 2024 season; say the word for a different scope" — so a
wrong guess costs one sentence to redirect, not a whole rebuild.

Restating the *model you already chose* is not alignment. "Here's the
hierarchical model I fit" answers nothing if the user hasn't agreed that a
model is what they wanted. Confirm the goal, not your output.

**Worked example — the right first response to an under-specified ask:**

> **User:** "Build me a marimo notebook analyzing F1 driver overtaking at corners."
>
> **You (no data, ambiguous scope, multiple framings → ask, don't build):**
> "Happy to build that. Two quick things so I build the right one:
> 1. **Data** — I can pull real results from a public source (e.g. the Ergast/
>    Jolpica F1 API or OpenF1), or you can attach a dataset. Which do you prefer?
> 2. **Scope & angle** — all 2024 GPs, a specific circuit, or multiple seasons?
>    And are you after *where* passes happen (corner-by-corner), *who* overtakes
>    most, or *what track features* drive overtaking? I'd start with corner-level
>    counts from real results — say the word and I'll pull the data and build it."

That single message is the difference between a demo and a useful tool. Note it
offers to do the real work (fetch real data) — it doesn't punt the effort back
to the user, it just confirms direction before spending a build.

## 2. Never fabricate data silently

This is the cardinal rule. A notebook that invents its own data, then reports
conclusions baked into that invention, launders your assumptions into
"findings." If you hardcode `drs_corner_success = 0.85` into a generator and
the notebook then concludes "DRS corners have the highest success rate," that
is circular — you wrote the answer, generated data to match, and read it back
as a discovery. A data scientist will (rightly) distrust everything you
produce after seeing this once.

**You have two ways to use a library outside the baked set, and neither is
fabrication.** The notebook runtime ships `marimo`, `pandas`, `numpy`, `scipy`,
`scikit-learn`, `matplotlib`, `pymc`, and `arviz` by default. For anything else
(e.g. `fastf1`):

- **Declare it in a PEP 723 header** — the notebook installs it into its own
  isolated env (see "Declaring extra dependencies" below). Use this when the
  notebook itself needs the library's *code* to run.
- **Or fetch the data in bash** — your **bash tool has full network access and
  can install anything**. Fetch and clean the real data with whatever libraries
  you need, then mint an attachment upload URL (`create_attachment_upload_url`)
  and curl the result up; the notebook reads `data/<name>`. Use this when you
  only need the *data* a library produces — especially for heavy or slow fetches
  you don't want re-run on every reactive re-render.

Either way, getting real data is almost always possible; reach for it first.

**Rules, in priority order:**

1. **Prefer real data.** Fetch it in bash (APIs, public datasets), attach it,
   read it in the notebook. If you can't source it, ask the user for it.
2. **Synthetic data is allowed ONLY with explicit user consent — which you may
   not grant yourself.** Consent means: the user literally asked for a
   demo/template/mockup, OR you asked under §1 and they said yes. An open-ended
   request like "analyze F1 overtaking" or "visualize churn" is a request *about
   the real world* — it is the §1 "no data" trigger, NOT permission to
   synthesize. "Synthetic-but-realistic, no API needed" is you granting yourself
   consent; that is the banned move. When you do have consent, the synthetic
   data must be unmistakably labelled — and honest:
   - Say "this uses synthetic/illustrative data" in your chat reply **and** in
     the notebook's title or first cell. Not a blockquote three lines down — up
     front, where it can't be missed.
   - **Do not state any result that is merely a parameter you chose.** Synthetic
     notebooks demonstrate *mechanics and interactivity* — "here's how this
     dashboard would look and behave" — never empirical claims about the real
     world. Write takeaways as "this demo shows the layout/interaction," never
     "X causes Y" or "X ranks highest." A line like *"Key findings: hairpins have
     the highest overtake volume"* when you set the hairpin rate yourself is the
     banned circular claim — even labelling it "baked into the data model" does
     not make it OK. Delete it, or restate it as "the demo is configured so
     hairpins show the most volume — swap in real data to find the truth."
3. **When data is partial** (e.g. you have real aggregate results but model the
   per-item breakdown), say so plainly and at the point of use, not buried in a
   footnote. Label the modelled layer "estimated," not "measured."

## 3. Validate before you publish

The host **runs your notebook before serving it**: it executes every cell, and
if any fails the curl response is HTTP 422 carrying a list of cell errors —
the broken notebook is never served. A 200 response with `url` in the JSON
means every cell actually executed.

When the curl returns a 422 with "notebook failed validation — cells did not
execute" and a list of errors, **read them, fix the source, mint a fresh upload
URL (each URL is single-use), and re-upload.** Do not surface the raw validation
error to the user as if the task failed — it's yours to fix. The most common
entry is `MultipleDefinitionError` (a name, often a loop variable, defined in
two cells): fix it with the function-wrapping pattern below.

You don't need to self-run the notebook first — the host does it for you. But if
you want to catch errors before spending a publish (e.g. you're iterating fast),
you can export it locally; a clean export means the cells execute:

```bash
uv run --with marimo --with pandas --with numpy --with scipy \
  --with scikit-learn --with matplotlib \
  marimo export html /tmp/nb.py -o /tmp/nb_check.html
```

For a notebook with a PEP 723 header, check it with `--sandbox` instead so the
declared deps get installed:

```bash
uv run --with marimo marimo export html --sandbox /tmp/nb.py -o /tmp/nb_check.html
```

A bare `python -c "ast.parse(...)"` syntax check is **not** a substitute — it
cannot see `MultipleDefinitionError` or any runtime failure. Add
`--with pymc --with arviz` when the notebook uses them. Note the host's
validation has a time budget: a notebook doing heavy `pm.sample` may be
published without a full execution check, so still keep sampling small (below).

## Publishing: upload URL + curl, never tool arguments

You do **not** paste notebook source or data into a tool argument. Large source
truncates and ~1 MB data files can't be base64'd through a tool call at all.
Instead you mint a **one-time upload URL** and `curl` your file to it from bash —
the bytes go straight to the host, never through the model.

1. **Get the `.py` into a sandbox file.** Either author it incrementally with
   write/edit and then `read` it back to confirm it's complete, or `curl` it from
   an origin. Never try to emit the whole notebook in one shot.
2. **Mint the upload URL:** `create_notebook_upload_url(slug="churn")` returns
   `{upload_url, slug, upload_expires_at}`. The URL is good for ~5 minutes and is
   single-use — use it promptly, and mint a fresh one for every retry.
3. **Upload:**
   ```bash
   curl -sS -X PUT --data-binary @nb.py "<upload_url>"
   ```
   The curl response is JSON carrying the live `url` — share that. On a 422 it
   carries the failing cells; fix them, mint a fresh URL, re-upload.

Slugs must match `[A-Za-z0-9_-]{1,32}` and not start with `-`. Use a short
human-readable name (`"churn"`, `"mmm-prior-check"`) — the server namespaces it
per user, so two users picking the same slug never collide. **Pass the same slug
to re-upload in place**: the URL stays stable across iterations, which keeps the
user's browser tab working. Re-uploading restarts the kernel, so in-browser
state (filter selections, scroll position) resets.

### Precompute, never sample live

A permanent blog runs a **real kernel per reader, and every cell executes on
page load.** A cell that calls `pm.sample(...)` makes *every visitor* wait
minutes, and concurrent readers each spawn their own sampler — which exhausts
host memory. So do all heavy computation **offline, before publishing**:

1. Run the expensive work once in bash and save the artifact:
   ```python
   idata = pm.sample(2000, tune=1000)
   idata.to_netcdf("posterior.nc")
   ```
2. Attach it under the **same slug** you'll publish under (see below).
3. In the notebook, **load** it and do only cheap work:
   ```python
   import arviz as az
   idata = az.from_netcdf("data/posterior.nc")   # cheap
   # interactive widgets explore the posterior — no resampling
   ```

A blog that re-samples on load is a broken blog. The same advice makes a scratch
notebook pleasant rather than painful.

### Writing a blog worth keeping

- **Write it as an article, not a code dump.** Lead with prose (`mo.md(...)`),
  interleave figures and interactive widgets, read top-to-bottom.
- **Keep every cell cheap** — loads, slicing, plotting, `arviz` over the
  precomputed `InferenceData`. Reactive widgets recompute plots from data
  already in memory, never refit models.
- **Use `width="medium"`** in the marimo app config, not `"wide"`.

## Declaring extra dependencies (PEP 723)

To use a library outside the baked set, put a PEP 723 script header at the very
top of the source (before `import marimo`). The host detects it and runs the
notebook in an isolated environment with those packages installed:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo", "fastf1", "pandas"]
# ///
import marimo

app = marimo.App()
# ... cells ...
```

- **Always list `marimo`** (and every other library you import). The isolated
  env *replaces* the baked one — it does not extend it — so a header that omits
  `pandas` while the notebook imports pandas fails validation.
- **Keep the list lean.** The first publish installs these before the notebook
  can run. A small stack (`fastf1` + `pandas`) is a few seconds; a heavy one
  (e.g. `torch`) can blow the validation time budget — the notebook then ships
  unverified and slow to first load.
- **No header → baked env.** Omit the header and the notebook runs on the fast
  default stack with no install step.
- **Heavy runtime fetching still belongs in bash.** A header lets the notebook
  *import* `fastf1`, but a cell that downloads large telemetry re-runs that
  download on every reactive re-render and can hit the subprocess memory/CPU
  caps. For big or slow data, fetch once in bash and attach the result.

## Cell dataflow rules (this is the #1 source of broken notebooks)

marimo runs cells as a reactive dataflow graph, not top-to-bottom like Jupyter.
Three rules, if violated, make cells **silently refuse to run** even though
the notebook uploaded successfully.

### Rule 1 — every name is defined in exactly one cell

This includes **loop variables, comprehension variables, and lambda
parameters**. A `for ax in axes:` in one cell and `for ax in other:` in another
collide — *both* cells break, and so does anything downstream of them. This is
the most common way a notebook ships broken.

**The robust fix: wrap each cell's body in a function so its locals never
leak.** Do this by default for any cell containing a loop or comprehension:

```python
# ✅ CORRECT — function-local names can't collide across cells
@app.cell
def _(posteriors):
    def build_table(posts):
        return {d: p["mu"] for d, p in posts.items()}
    table = build_table(posteriors)
    return (table,)
```

```python
# ❌ BROKEN — `drv`/`p` leak; reused in another cell → both cells refuse to run
@app.cell
def _(posteriors):
    table = {drv: p["mu"] for drv, p in posteriors.items()}
    return (table,)
```

Only the names you `return` escape the function-wrapped cell — exactly what you
want. Reserve module-level cell names for the values you actually export.

### Rule 2 — `return` is only legal as a cell's final statement

Mid-body `return` is a SyntaxError at marimo parse time and the cell produces no
output. Guard with `if/else` and return at the end:

```python
# ✅ CORRECT
@app.cell
def _(df):
    result = df.mean() if not df.empty else None
    return (result,)
```

### Rule 3 — the signature lists names read; the return tuple lists names defined

```python
@app.cell
def _(mo, df):          # ← names READ from upstream cells
    chart = df.plot()
    return (chart,)     # ← names EXPORTED to downstream cells
```

Forget a name in the return tuple and downstream cells can't see it. List a name
in the signature that no upstream cell exports and the cell errors.

## Data attachments

Use `create_attachment_upload_url(slug, name)` to get a one-time upload URL for
a data file, then `curl` the file to it. The file becomes readable from inside
the published notebook as `data/<name>` — always that path, regardless of how it
arrived.

Pass the **same `slug`** to `create_attachment_upload_url` and
`create_notebook_upload_url` to bind them. Different slugs are different
workspaces with isolated data directories; a notebook published under one slug
cannot see another's attachments.

```python
import pandas as pd
df = pd.read_csv("data/sales.csv")
```

Attachment data lives and dies with its workspace: on a scratch notebook it goes
when the notebook expires. If a user expects long-term storage, tell them this
isn't the right tool.

Per-attachment cap is 10 MiB (operator-configurable). Larger files: ask the user
to subsample or aggregate before sending. Attach and publish share a single
per-principal hourly rate-limit budget, so a loop that attaches → publishes →
attaches → publishes burns the budget twice as fast as one that publishes alone.
If `create_attachment_upload_url` raises a tool error containing "not configured"
or "rate limit", surface that to the user — don't retry blindly.

### Worked example — a file the user attached in chat

The attachment reaches you as an `[attachment]` line carrying a signed URL, not
as a file on disk. Fetch it, then attach it to the workspace:

```
# User (with sales.csv attached): "load this CSV and plot column A"
# [attachment] `sales.csv` (1024 bytes), uploaded by the user with this message.
#   Signed Discord CDN URL, expires ~24h: https://cdn.discordapp.com/... — curl it
#   to disk, then read it. To use it in a notebook you publish, upload it via the
#   create_attachment_upload_url tool.
```
```bash
curl -sS "<the signed URL>" -o /tmp/sales.csv
# create_attachment_upload_url(slug="sales-explore", name="sales.csv") -> {upload_url, ...}
curl -sS -X PUT --data-binary @/tmp/sales.csv "<upload_url>"

# Write the notebook (it does pd.read_csv("data/sales.csv")), verify it, then:
# create_notebook_upload_url(slug="sales-explore") -> {upload_url, ...}
curl -sS -X PUT --data-binary @nb.py "<upload_url>"
```

The same shape is the antidote to fabrication: fetch real data in bash with
whatever library you like, attach the result, and let the notebook read it.

## PyMC / ArviZ notebooks

The runtime ships **PyMC 5.x and ArviZ 0.x** (pinned — do not assume PyMC 6 or
ArviZ 1.x entry points; use the 0.x surface like `az.plot_posterior`,
`az.plot_trace`, `az.summary`). Bayesian work is a first-class use of this tool,
not a fallback.

The notebook runs in a **resource-capped subprocess** (memory + CPU limits).
Real MCMC there must be modest, or the kernel gets killed mid-sample:

- Keep `pm.sample(...)` small: a few hundred to ~1–2k draws, `tune` similar,
  `cores=1` (the cap makes multi-core a liability, not a speedup), and
  `progressbar=False`.
- For anything heavier, sample in bash, save the `InferenceData`
  (`az.to_netcdf`), attach it, and load it with
  `az.from_netcdf("data/idata.nc")` to render diagnostics.
- A notebook that re-samples on every reactive re-run is painful to use — fit
  once in an early cell (or load attached `InferenceData`), explore downstream.

## Managing what you published

- `list_notebooks()` — everything you published, scratch and permanent. Each
  entry carries `slug`, `url`, `alive`, `permanent` and, for a scratch notebook,
  `expires_at`. `url` is null while a notebook is stopped; its link still works.
- `delete_notebook(slug)` — un-publish it and delete its files from the host.
  Delete what nobody needs rather than leaving a long `ttl_days` to run out.

The `slug` these report is the **bare** name — the same one you pass to
`create_notebook_upload_url`. Pass it straight back to `delete_notebook`; don't
reconstruct a slug from the URL, which carries an extra namespace segment.

`delete_notebook` returns `{slug, deleted}`. `deleted: false` means nothing by
that name existed — check `list_notebooks` for the right slug rather than
retrying the same call.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Curl returns 422 with cell errors | A cell genuinely fails to execute | Read the errors, fix the source, mint a **fresh** URL (single-use), re-upload |
| `MultipleDefinitionError` | A name — often a loop variable — defined in two cells | Wrap each cell body in a function so locals don't leak (Rule 1) |
| A cell produces no output at all | Mid-body `return` | Move the `return` to the last statement (Rule 2) |
| Downstream cell can't see a value | Name missing from the return tuple | Add it (Rule 3) |
| `ModuleNotFoundError` despite a PEP 723 header | The header omits a library the notebook imports — the isolated env replaces the baked one | List **every** import, `marimo` included |
| Blog takes minutes to load, or dies under two readers | Sampling on page load | Precompute in bash, attach the `InferenceData`, load it in the notebook |
| Upload URL rejected on retry | Each URL is single-use, and expires in ~5 min | Mint a new one per attempt |
| `delete_notebook` returns `deleted: false` | Wrong slug — most often a URL-derived one carrying the namespace prefix | Take the bare slug from `list_notebooks` |
| ToolError "notebook host not configured" | This deployment has no notebook host | Tell the user, show the source as a code block, don't retry |
