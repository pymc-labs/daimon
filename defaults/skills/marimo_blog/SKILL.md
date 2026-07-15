---
name: marimo_blog
description: Publish a marimo notebook as a PERMANENT, shareable blog via the daimon MCP server. You mint a one-time upload URL with create_blog_upload_url and curl your notebook file to it — the source never goes through a tool argument. The blog is a live, read-only marimo app (interactive widgets work, code hidden), hosted indefinitely. Use when someone asks to turn a notebook/analysis into a blog post, publish it, or make it shareable.
---

# marimo_blog

Publish a marimo notebook as a **permanent, shareable blog**. Unlike an ephemeral
notebook, a blog is:

- **Permanent** — survives host restarts, never expires.
- **Read-only (run mode)** — readers see a live app: prose, figures, interactive
  widgets. Source is hidden. No editor.
- **Shareable** — the slug is part of the public URL.

If you have not already, read the `marimo_notebooks` skill first — it covers
building a *good* notebook (aligning on the question, never fabricating data,
layout). This skill adds only what's different about a blog.

## How content reaches the host: upload URL + curl (NOT tool arguments)

You do **not** paste notebook source or data into a tool argument. Large source
truncates and ~1 MB data files can't be base64'd through a tool call at all.
Instead you mint a **one-time upload URL** and `curl` your file to it from bash —
the bytes go straight to the host over the network, never through the model.

The flow for every blog:

1. **Get the `.py` into a sandbox file.** Either author it incrementally with
   write/edit and then `read` it back to confirm it's complete, or `curl` it from
   an origin (`curl -sS <origin>/notebook.py -o blog.py`). Never try to emit the
   whole notebook in one shot.
2. **Mint the upload URL:** call `create_blog_upload_url(slug="radar-plots")`.
   It returns `{upload_url, slug, upload_expires_at}`. The URL is good for ~5
   minutes — use it promptly.
3. **Upload the file:**
   ```bash
   curl -sS -X PUT --data-binary @blog.py "<upload_url>"
   ```
   The curl response is JSON. On success it carries the live `url` — share that.
   On a 422 it carries the failing cells; fix them, mint a fresh URL (each URL
   is single-use), and re-upload.

## The one rule that matters most: precompute, never sample live

A blog runs a **real Python kernel per reader, and every cell executes on page
load.** A cell that calls `pm.sample(...)` makes *every visitor* wait minutes and
concurrent readers each spawn their own sampler — which exhausts host memory.

**So do all heavy computation OFFLINE, before publishing, and load the result.**

1. In your bash session, run the expensive work once and save the artifact:
   ```python
   idata = pm.sample(2000, tune=1000)
   idata.to_netcdf("posterior.nc")
   ```
2. Upload it into the blog's workspace under the **same slug** you'll publish under.
   Mint a data upload URL and curl the file up:
   ```bash
   # create_attachment_upload_url(slug="radar-plots", name="posterior.nc") -> {upload_url, ...}
   curl -sS -X PUT --data-binary @posterior.nc "<upload_url>"
   ```
   The file becomes available inside the blog as `data/posterior.nc`.
3. In the blog notebook, **load** the precomputed artifact and do only cheap work:
   ```python
   import arviz as az
   idata = az.from_netcdf("data/posterior.nc")   # cheap
   # interactive widgets explore the posterior — no resampling
   ```

A blog that re-samples on load is a broken blog.

## Authoring a blog notebook

- **Write it as an article, not a code dump.** Lead with prose (`mo.md(...)`),
  interleave figures and interactive widgets, read top-to-bottom.
- **Keep every cell cheap** — loads, slicing, plotting, `arviz` over the
  precomputed `InferenceData`. Reactive widgets (`mo.ui.slider`, `mo.ui.dropdown`)
  recompute plots from data already in memory, never refit models.
- **Interactivity is the point.** A live kernel (not WASM) means PyMC/ArviZ
  widgets genuinely work — sliders that re-plot a posterior, dropdowns that switch
  parameters. That's what a blog buys over a static export.
- **Use `width="medium"`** in the marimo app config, not `"wide"`.
- **Extra dependencies:** if the blog needs a library outside the host's baked
  stack, declare it in a PEP 723 header at the top of the source:
  ```python
  # /// script
  # requires-python = ">=3.12"
  # dependencies = ["marimo", "arviz", "matplotlib"]
  # ///
  ```

## Publishing checklist

1. Precompute + attach any data files first (`create_attachment_upload_url` →
   curl), under the slug you'll publish.
2. Get the blog `.py` into a sandbox file (author + read-back, or curl from origin).
3. `create_blog_upload_url(slug=...)` → `curl -X PUT --data-binary @blog.py "<url>"`.
   - Choose a **meaningful, stable slug** — it's part of the permanent public URL.
   - The host validates the cells execute before serving. On a 422, fix the named
     cells, mint a fresh URL, re-upload (re-uploading the same slug replaces the
     running blog in place).
4. Share the `url` from the curl response.

## Managing blogs

- `list_blogs()` — see the blogs you've published (slug, url, whether live).
- `delete_blog(slug)` — un-publish and free its host port. Each *published* blog
  holds one port from a finite pool (readers don't each consume a port). Delete
  blogs you no longer need.

## What this skill does NOT do

- No WASM export, no static HTML, no external CMS, no image hosting. The blog is
  the live notebook served by the host.
- No live model fitting. Precompute and attach. (See the rule above.)
- No pasting source or data into tool arguments. Always upload via curl.
