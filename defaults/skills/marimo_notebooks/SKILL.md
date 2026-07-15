---
name: marimo_notebooks
description: Publish a marimo notebook for the user via the daimon MCP server's create_notebook_upload_url tool — you mint a one-time upload URL, get the notebook's .py into a sandbox file, and curl -X PUT --data-binary it to the URL (the source never goes through a tool argument, which truncates large notebooks). The curl response returns a slug-as-secret URL the user can open.
---

# marimo_notebooks

Use the daimon MCP server's `create_notebook_upload_url` tool to render and
publish an interactive marimo notebook for the user. You mint a one-time upload
URL, get the notebook's `.py` into a sandbox file, and `curl -X PUT --data-binary`
it to the URL — the source never goes through a tool argument, which truncates
large notebooks. The curl response returns a slug-as-secret URL that the user can
open directly in a browser. Treat that URL as private — share it only with the
user who asked.

A notebook is data work, not decoration. The person on the other end is
usually trying to answer a real question. A polished notebook that answers
the *wrong* question, or that "discovers" conclusions you secretly invented,
or that errors on the first cell, is worse than no notebook — it looks
authoritative while being hollow. The three rules below exist to prevent
exactly that. Read them before you build anything.

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
the broken notebook is never served. A 200 response with `url` and `expires_at`
in the JSON means every cell actually executed.

When the curl returns a 422 with "notebook failed validation — cells did not
execute" and a list of errors, **read them, fix the source, mint a fresh upload
URL (`create_notebook_upload_url` — each URL is single-use), and re-upload.**
Do not surface the raw validation error to the user as if the task failed — it's
yours to fix. The most common entry is `MultipleDefinitionError` (a name, often
a loop variable, defined in two cells): fix it with the function-wrapping pattern
below.

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
  default stack with no install step. Only add a header when you need something
  outside `pandas`/`numpy`/`scipy`/`scikit-learn`/`matplotlib`/`pymc`/`arviz`.
- **Heavy runtime fetching still belongs in bash.** A header lets the notebook
  *import* `fastf1`, but a cell that downloads large telemetry re-runs that
  download on every reactive re-render and can hit the subprocess memory/CPU
  caps. For big or slow data, fetch once in bash and attach the result (§2)
  rather than fetching live in the notebook.

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

## PyMC / ArviZ notebooks

The runtime ships **PyMC 5.x and ArviZ 0.x** (pinned — do not assume PyMC 6 or
ArviZ 1.x entry points; use the 0.x surface like `az.plot_posterior`,
`az.plot_trace`, `az.summary`). Bayesian work is a first-class use of this tool,
not a fallback.

The notebook runs in a **resource-capped subprocess** (memory + CPU limits, ~2h
TTL). Real MCMC there must be modest, or the kernel gets killed mid-sample:

- Keep `pm.sample(...)` small: a few hundred to ~1–2k draws, `tune` similar,
  `cores=1` (the cap makes multi-core a liability, not a speedup), and
  `progressbar=False`.
- For anything heavier, sample in your **bash tool** instead, save the
  `InferenceData` (`az.to_netcdf`), mint an attachment upload URL
  (`create_attachment_upload_url`) and curl it up, then have the notebook load
  it via `az.from_netcdf("data/idata.nc")` to render diagnostics. The notebook
  then visualises real results without paying the sampling cost at load time.
- A notebook that re-samples on every reactive re-run is painful to use — fit
  once in an early cell (or load attached `InferenceData`), explore downstream.

## Iterating on the same notebook

When refining a notebook in the same conversation, pass the same `slug` to
`create_notebook_upload_url(slug=prior_slug)` so the user keeps the same browser
tab. The URL is stable across re-uploads of the same slug:

```python
# 1. update nb.py with your edits (write/edit tools, then read back to verify)
```
```bash
# create_notebook_upload_url(slug="churn")  ->  {upload_url, slug, upload_expires_at}
curl -sS -X PUT --data-binary @nb.py "<upload_url>"
# curl response JSON carries url + expires_at — same url as before; notebook is now updated
```

Slugs must match `[A-Za-z0-9_-]{1,32}` and not start with `-`. Use a short
human-readable name (`"churn"`, `"mmm-prior-check"`) — the daimon server
namespaces it per user so two users picking the same slug never collide.

Note: re-uploading restarts the notebook's kernel, so any in-browser state
(filter selections, scroll position) resets. The URL stays the same.

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
# or: open("data/raw.json")
```

Data is **ephemeral**: it dies with the notebook subprocess TTL reap (default
2 hours). No persistence, no cross-conversation sharing. If a user expects
long-term storage, tell them this isn't the right tool.

Per-attachment cap is 10 MiB (operator-configurable). Larger files: ask the user
to subsample or aggregate before sending. No streaming uploads in v1.

Attach and publish share a single per-principal hourly rate-limit budget. A loop
that attaches → publishes → attaches → publishes burns the budget twice as fast
as one that publishes alone.

When a user attaches files to a Discord message, the bot uploads them for you and
prepends a system line listing each attachment's slug and path. **Reuse that
slug** when calling `create_notebook_upload_url` to include the data.

### Worked examples

**(a) Discord auto-upload — you only publish.** The system line gives you the
slug; bind to it:

```
# User (Discord, with sales.csv attached): "load this CSV and plot column A"
# System: *user attached `sales.csv` (1024 bytes) at `data/sales.csv` on notebook workspace `abc123...`. Use slug=abc123... when publishing to include it.*

# 1. Write the notebook to nb.py (write/edit tools), then read it back to verify it's complete.
```
```bash
# create_notebook_upload_url(slug="abc123...")  ->  {upload_url, slug, upload_expires_at}
curl -sS -X PUT --data-binary @nb.py "<upload_url>"   # response JSON carries url + expires_at
```

**(b) MCP-only client (Claude Desktop, Cursor, ...) — you attach data, then publish.**
Pick a slug, use it for both calls:

```
# User: "here's the CSV data: save it to /tmp/sales.csv. plot column A."

# 1. Save the data to a sandbox file, then:
```
```bash
# create_attachment_upload_url(slug="my-csv-explore", name="sales.csv")  ->  {upload_url, ...}
curl -sS -X PUT --data-binary @/tmp/sales.csv "<upload_url>"

# 2. Write notebook to nb.py (reads data/sales.csv), verify it, then:
# create_notebook_upload_url(slug="my-csv-explore")  ->  {upload_url, ...}
curl -sS -X PUT --data-binary @nb.py "<upload_url>"
```

**(c) You fetched real data in bash — attach it, then publish.** This is the
antidote to fabrication: get the real thing, hand it to the notebook.

```bash
# In bash: fetched + cleaned real data into /tmp/races.csv (any library you like).
# create_attachment_upload_url(slug="f1-overtaking", name="races.csv")  ->  {upload_url, ...}
curl -sS -X PUT --data-binary @/tmp/races.csv "<upload_url>"

# Write notebook to nb.py (does pd.read_csv("data/races.csv")), verify it, then:
# create_notebook_upload_url(slug="f1-overtaking")  ->  {upload_url, ...}
curl -sS -X PUT --data-binary @nb.py "<upload_url>"   # response JSON carries url + expires_at
```

### Attachment errors

If `create_attachment_upload_url` raises a tool error containing "not configured"
or "rate limit", surface that to the user — don't retry blindly. The rate-limit
budget is shared with `create_notebook_upload_url`, so retries make it worse.

## When to use

- User asks for an interactive notebook, dashboard, or data explorer.
- Dataset exploration with reactive sliders, dropdowns, or filters.
- Visualisation tasks where a static image isn't enough (e.g. matplotlib +
  marimo widgets, altair interactive charts).

## Notebook source shape

The notebook is a complete marimo `.py` file — written to a sandbox file, then
curled to the upload URL. Minimal example:

```python
import marimo

app = marimo.App()

@app.cell
def _():
    import marimo as mo
    return (mo,)

@app.cell
def _(mo):
    mo.md("# Hello from marimo")
    return ()

if __name__ == "__main__":
    app.run()
```

Keep cells small and independent. Do not use `marimo.run()` or blocking calls;
marimo drives execution. Four to six cells is enough for most tasks.

## Constraints

- **Notebook runtime imports** default to `marimo`, `pandas`, `numpy`, `scipy`,
  `scikit-learn`, `matplotlib`, `pymc`, `arviz`. To use anything else, declare a
  PEP 723 header (see "Declaring extra dependencies") and the notebook installs
  it into an isolated env — or fetch the data in bash and attach it (§2). Both
  beat the old "show the code in chat and have the user run it locally" fallback.
- Notebooks are **ephemeral**: the host may reap a subprocess after ~2h of
  uptime or on host restart. Tell the user to export their work before the
  session ends. The curl response JSON carries an `expires_at` — surface it.
- URL is **slug-as-secret** — the slug is the only access boundary. Share only
  with the user who asked; never post it in a public channel.

## Errors

If `create_notebook_upload_url` raises `ToolError("notebook host not configured")`:
tell the user interactive notebooks aren't available in this deployment and show
the notebook source as a code block in chat instead. Do not retry.

If a publish fails for another reason (timeout, host unreachable, upstream
error), don't leave the user empty-handed: tell them it failed and paste the
notebook source you built as a code block so the work isn't lost.
