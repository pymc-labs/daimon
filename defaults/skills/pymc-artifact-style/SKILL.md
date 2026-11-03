---
name: pymc-artifact-style
description: Apply PyMC Labs' house style and the readability and clarity principles to every artifact you produce — reports, PDFs, slide decks, charts, images, notebooks, guides, FAQs, demos and walkthroughs. Use whenever you generate something a person will look at, and always before anything is sent, shown or published to a client or prospect, including a demo, a proposal or a guided tour.
---

# PyMC Labs artifact style

Everything you hand a user is a PyMC Labs deliverable and should look like one:
a well-edited document, not an app. The reader is busy, often on a phone, and
wants the answer first. This is PyMC's own style; never substitute a client's
brand colors unless the user explicitly asks for a client-branded artifact.

The look in one line: Inter and JetBrains Mono, navy text on white, one accent
(indigo) for links and the prompt Copy button, one full-width column, large body
text, headings that state the point, rules-only tables, a small wordmark. The
bundled templates produce exactly this; start from them rather than from a blank
page, and run the pre-publish lint before any HTML goes out.
Read [references/website-brand.md](references/website-brand.md) for where the
palette and type come from.

Keep titles and labels short and copy sparse. Reports still need enough
evidence, units, and uncertainty to support their conclusions. Reuse the bundled
official logos; if new logos or artwork are explicitly requested, use image
generation.


## Readability and clarity — anything a client or prospect will see

Anything sent, shown or published to a client or prospect counts: reports,
notebooks, guides, FAQs, demos, guided tours, proposals and decks. What follows
is principles with the reasoning behind them, not a template. The content decides
the shape. Use the reasoning to make the call each time.

### Principles and why they matter

- **Know the reader and the decision before writing.** The same facts make a
  different document for a VP choosing a budget than for an analyst running a
  model. Writing before you know which one you're writing for produces a document
  that serves neither. If the reader or the decision is unclear, ask one
  question, not a questionnaire, and give your best guess alongside it. When a
  request will drown its reader, say so and offer the tighter version alongside
  the one they asked for, so they can choose.
- **Lead with what changes for the reader.** A client decides in the first screen
  whether to keep reading. If that screen is navigation, setup or caveats, they
  conclude the document is about us, not them. Open with the outcome, the decision
  it supports, or what they can do now.
- **Signal over volume.** The reader pays in attention. Every sentence that carries
  nothing they would act on or miss spends that attention and hides the parts that
  matter. A long document reads as thorough to its writer and as work to its
  reader.
- **Say each thing once.** Repetition tells the reader we didn't edit. It also
  teaches them to skim past text, so the one sentence that differs gets skipped.
  Repeated disclaimers, setup restated in every step, and the same label on every
  card are the usual culprits.
- **Make every part readable on its own.** Clients don't read front to back. They
  jump to the part their boss asked about. A part that only makes sense after the
  previous three sections fails that reader.
- **Use plain language and define what remains.** The client is often senior and
  not a specialist, and forwards the document to someone even less so. Jargon makes
  them feel the document is not for them, and parameter detail in the main read
  suggests we are showing off rather than helping.
- **Give numbers with context.** A number without units, a comparison or its
  uncertainty can't be acted on, and a wrong reading of it is our fault.
- **Be consistent.** "30 steps" on a page that numbers 31, or a voice that flips
  between ours and theirs, signals no one checked. The client then wonders what
  else no one checked.
- **Do the work rather than hand it over.** "Check this against your own records"
  passes our job to the person paying for it.

### Judgement calls

- **Prefer short by default.** For client and demo sends with no explicit size,
  the shortest version that does the job usually wins. A prospect trying a demo
  wants one early success, not a curriculum.
- **When long is justified:** when the requester explicitly wants depth (a full
  handbook, an internal deep dive), or the content can't be made shorter without
  losing something the reader needs. Then give them the length, keep it tight, and
  make it easy to navigate. Never pad to look substantial, and never refuse a big
  request.
- **What counts as noise:** filler and throat-clearing; boilerplate labels;
  restated context; setup repeated across steps; hedges that protect us but don't
  inform them; decorative mock-ups; meta-commentary about how the document was
  made.
- **How to cut:** for each part ask "what would the reader lose if this went?".
  If the answer is nothing, or that it is said elsewhere, it goes. Merge parts
  that make the same point. Move technical detail into code or a clearly technical
  part, where the reader who wants it can find it.
- **Requests that push toward volume** ("include everything", "add every thread",
  "make it longer"): find what the person actually needs from the extra material
  and fold it in where it belongs. When a revision adds material, it should make
  the document better, not just bigger.

### Correctness over polish

One wrong claim a client catches costs the trust that every right one built, and a
well-formatted wrong claim is worse, because it looks checked.

- Claim nothing beyond the evidence. If something was not run, measured or
  sourced, say what it is: a plan, an assumption or an illustration.
- Keep sandbox and simulated work visibly separate from real results, and label
  it where it appears. A number from synthetic data that reads like a finding
  about the client's business will get quoted as one, and excerpts travel without
  their context.
- Flag weak methods instead of smoothing them over. The reader can't see the
  method, so they trust us to tell them when it is thin.
- Don't call a fit reliable on the strength of its settings. Whether there are
  enough draws is for the diagnostics to say (effective sample size, R-hat,
  divergences), and passing them shows the sampler worked, not that the model is
  right. A few dozen posterior draws carried into a second stage is a smoke test,
  not a result.
- Don't validate in a circle. Calibrating on a lift test simulated from the model's
  own truth shows the plumbing works, not that calibration works. A real
  experiment's result can't be simulated in advance. If it could, no one would run
  the experiment.
- "Tested against a known truth" is true only on simulated data. Saying it of real
  data or of the method in general overstates what anyone can know.
- Promise what you checked, not completeness ("a source for every point"). One
  missing source then breaks the whole promise.

### Rewrite pass before sending (mandatory)

Reread the whole thing as the client: senior, busy, and paying for this. This
pass is about whether the document honours the principles above, not about its
format. Cut the noise. Make sure the point lands early and that any part can be
read on its own. Fix every claim that fails. Then ask the real question: would
this client pay for what they just read? If not, rewrite before sending.

### Send-readiness note (advisory)

When you hand a colleague a document meant for a client, add a short
send-readiness note to your reply to them, after the deliverable: who the reader
is, what you would still cut or check (including whether a modeller should review
any modelling advice), and the two or three questions the client is likely to
ask. The requester usually knows the client better than you do, and this is the
fastest way to let them catch what you can't. It is advice and never blocks the
send. Skip it when the person you are talking to is the client.

## The look — what every artifact uses

| Element | Rule |
|---|---|
| Type | **Inter** for everything, **JetBrains Mono** for code. Body 18–19px on screen (16pt in a PDF), line height 1.6. Headings 600 at 40 / 28 / 22px (32 / 25 / 21 on a phone). |
| Color | Navy `#0C1F40` text on white. One accent, indigo `#5462C4`, for links and the prompt Copy button only. Secondary text is navy at 72% opacity. |
| Layout | One column, full width of the screen, up to about 820px for reading. No second column at any width in a document; a dashboard may set two charts side by side. |
| Headings | State the point: "Channel A returns more than Channel B", not "Results". A reader who skims only the headings gets the story. |
| Tables | Rules only: heavy top and bottom, a light header rule, no verticals, no fills, numbers right-aligned. |
| Charts | Only when they show real data from this work, with a heading or title that states what they show, units on the axes, intervals where a decision depends on them. |
| Prompts | A plain code block with one Copy button. Nothing else gets a copy button. |
| Wordmark | The dark PyMC Labs wordmark, about 20px tall, top left, on white. |
| Caveats | A plain sentence where they apply. Simulated or example data is said once, near the top, in words. |
| Footer | One line: PyMC Labs, the month, and the data note if there is one. |

Bold only as a leading label ("**You get:** …"), never scattered through
sentences.

## Never

- Hero bands, dark header blocks, decorative art, fake UI or fake chart mockups.
- Pills, badges, chips, emoji, icon-card grids, key-number cards, rounded-card layouts.
- Callout boxes, stacked callouts, "Good to know", "Key takeaways", "At a glance", "TL;DR".
- Accordions, expand / collapse buttons, tabs on a document (dashboards may use tabs).
- Sidebars, sticky or floating nav, link icons on headings.
- Buttons that do nothing for the reader (Start, Share, Download) and copy buttons on anything but a prompt.
- Gradients, shadows, a second accent color, an italic serif flourish in a title.
- "Prepared by … for …", "Confidential" unless legally required, eyebrow labels such as "GUIDE · CLIENT × PYMC LABS".
- Stock phrasing: "not just X but Y", "Here's the thing", "Let's dive in", "In today's …", lists of three for rhythm, intros that restate the title, summaries that repeat the page, "I hope this helps".

## Pre-publish lint — mandatory for every HTML artifact

Before you publish, send or share any HTML (page, guide, report, one-pager,
FAQ, dashboard), run:

```bash
python3 <this skill>/lint/publish_lint.py page.html                    # documents
python3 <this skill>/lint/publish_lint.py --kind dashboard dash.html   # dashboards
python3 <this skill>/lint/publish_lint.py --render page.html           # + text size and phone overflow (Playwright)
```

Exit code 1 means at least one **BLOCK** finding (accordions, sidebars, sticky
nav, hero bands, decorative SVG, gradients, shadows, pills, cards, icon-card
grids, callout boxes, emoji, serif accents, small body text, stray buttons,
copy buttons on non-prompts, boilerplate labels, stock phrasing, bold scattered
through a paragraph, styled words in the title). Each finding names the first
element or phrase it matched.
Fix every one and run it again; do not publish with a BLOCK. **WARN** findings
(softer phrasing such as "robust" or "leverage", generic headings, rounded
corners, eyebrows) are fixed too; keep one only when it is a false positive, and
say so in the send-readiness note. `lint/test_publish_lint.py` is the lint's own
test.

Exit code 2 means the lint could not run (for example, a wrong path).

The lint reads HTML pages and dashboards you author. It does not cover marimo
exports (their interface is drawn by JavaScript), PDFs or slides: those get
their look from the templates below and their writing from the rewrite pass, so
check them against the Never list yourself.

## Building each kind of artifact

### HTML pages — guides, reports, one-pagers, FAQs

Write the content as Markdown and build it; the page style is frozen in
`page/page.css`, so the output looks right by default.

```bash
S=<this skill>
python3 $S/page/build_page.py content.md -o page.html --footer "PyMC Labs, November 2026."
python3 $S/lint/publish_lint.py page.html
```

The Markdown subset is the whole vocabulary: one `# Title that states the
point`, the first paragraph after it becomes the lede, `##`/`###` headings,
paragraphs, lists, pipe tables (numeric columns right-align), ```` ```prompt ````
fences (a prompt with one Copy button), other fences (plain code, no button),
`![Caption](figure.png)` (inlined with the caption under it), `**bold**`,
`*italic*`, `` `code` `` and links. Fonts and the wordmark are embedded, so the
file renders offline (`--fonts google` links them instead, for a smaller page).
Worked examples, built in CI: `page/examples/guide.md` (a prompt walkthrough),
`report.md` (a short analysis with two charts from `make_figures.py`) and
`onepager.md` (a decision page for a VP).

### Reports and PDFs — `typst/pymc-plain.typ`

The Typst compiler, fonts and logo are bundled. The `typst` CLI is not
installed; use the Python package:

```bash
pip install typst
```

```python
import typst
typst.compile(
    "starter.typ",
    output="report.pdf",
    root="..",                # the skill dir, so ../assets and ../fonts resolve
    font_paths=["../fonts"],  # REQUIRED: without it headings silently go serif
)
```

`typst/starter.typ` is a working document: copy it and replace the content.

```typ
#import "pymc-plain.typ": *
#show: pymc-plain.with(
  title: [A title that states the finding],
  lede: [The answer in two sentences.],
  date: [November 2026],
  footer-note: [Example data, not a real business.],   // or none
  paper: "a4",                                         // or "us-letter"
)
= A heading that states the point
Body text …
```

It gives one full-width column at 16pt, the small wordmark, no cover page, no
table of contents, no margin notes. `#dtable(...)` is the rules-only table;
`#figure(image("fig.pdf"), caption: [...])` puts the caption under the figure.
A PDF that needs depth gets more sections, not more devices.

`typst/pymc-report.typ` (the older margin-note layout with cover, sidenotes and
key-figure callouts) and `report-example.typ` stay only for an explicit request
for that layout or for revising a document already built on it.

### Slide decks (.pptx) — render the slides, do not typeset them

There is no PowerPoint template. Typeset each slide in Typst at slide size
(white page, the small dark wordmark top left, one heading that states the
point, large text, at most one chart or table), render it to PNG, and place one
full-bleed image per slide. The fonts are baked into the pixels, so the style
survives the trip.

```python
import typst
from pptx import Presentation
from pptx.util import Inches

# 1. One Typst file per slide, 16:9 with no margin:
#      #set page(width: 13.333in, height: 7.5in, margin: 0pt)
typst.compile("slide1.typ", output="slide1.png", root="..",
              font_paths=["../fonts"], format="png", ppi=144)  # 144 → 1920x1080

# 2. Blank layout, picture at full bleed.
prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
for png in slides:
    s = prs.slides.add_slide(prs.slide_layouts[6])   # 6 is the blank layout
    s.shapes.add_picture(png, 0, 0, width=prs.slide_width, height=prs.slide_height)
prs.save("deck.pptx")
```

**Do not build slides out of pptx text boxes.** A text run stores the font by
name, the bundled fonts may not be installed where the file opens, and
PowerPoint and Google Slides silently substitute a generic sans. The cost of
rendering is real: the text is no longer editable. If the recipient needs to
edit the words, say so and hand over the Typst sources.

Use `assets/pymc-labs-logo-dark.png` on white, and
`assets/pymc-labs-logo-light.png` only on a dark slide. Preserve aspect ratio;
do not recolor or regenerate them.

### Marimo notebooks — build with the bundled style cell

The style cell gives a notebook the same look: Inter and JetBrains Mono
embedded, navy text, the small wordmark, plain headings, rules-only tables.
Marimo's theme variables are overridden, so headings do not fall back to
marimo's serif. Follow `marimo_notebooks` for everything else: alignment, real
data, cell dataflow, publishing and attachments.

The style cell is ~200 KiB with fonts and logo embedded. **Never paste it or
read it into the conversation.** Write only your content cells and let the
build script assemble the notebook:

```bash
S=<this skill's directory>/notebook
cp $S/example_content.py content.py                 # a working example to start from
mkdir -p data && cp $S/mmm_example.csv data/        # only to run the example itself
python $S/build_notebook.py content.py -o nb.py --title "Tab title"   # --width compact|medium|full
marimo export html nb.py -o /tmp/check.html         # every cell must execute
```

`content.py` holds `@app.cell` functions only (no `import marimo`, no
`app = ...`, no `__main__` block). The style cell defines
`mo np pd plt C CYCLE SEQ DIV badge callout figure header kpis table`: declare
the ones a cell uses in its signature and never redefine them. The notebook
host ships marimo, matplotlib, numpy and pandas; for anything else add a PEP 723
header to the built `nb.py` listing those four plus your extras.

1. `header(title, subtitle)` first: it also injects the page CSS. The title
   states the point; the subtitle is one plain line, and is where example data
   is said.
2. Sections as `mo.md("## Heading that states the point\n\ntext")`, then the
   controls in an `mo.hstack`, then the chart they drive.
3. Charts through `figure(fig, caption)`, tables through `table(df, fmt=...)`.

`badge()`, `callout()` and `kpis()` still exist so older notebooks build; they
now render plain text, a plain paragraph and a rules-only table. Do not use
them in new notebooks.

Keep it light: no dark theme. Example data is said once and never stated as a
finding. A headless browser
rendering a check needs `locale="en-US"`.

### Interactive dashboards — build with the bundled template

A dashboard is a report the client keeps using: white page, small wordmark,
title that states the point, tabs in the drill-down order **executive summary →
channel deep dive → model inventory**. Build single-file HTML from the skeleton:

```bash
python3 dashboard/build_dashboard.py content.html -o dashboard.html \
    --title "..." --subtitle "..." --scripts charts.js
python3 lint/publish_lint.py --kind dashboard dashboard.html
# content.html: only <section class="tab" id="…" data-title="…"> blocks
# charts.js:    Plotly calls — ALWAYS pass {...PYMC_LAYOUT} and PYMC_CONFIG
```

The template wires the tabs, inlines the wordmark, loads Plotly from the CDN,
and defines `C` (palette constants), `PYMC_SERIES`, `PYMC_LAYOUT`, `PYMC_CONFIG`.
Key numbers go in `<div class="keynums"><div class="keynum">` with `.knum`,
`.klabel` and `.kci` (a plain row with rules, not cards); a status word is
`<span class="status">`, plain semibold text. The builder refuses any hex that is not a palette
value. Interactivity is vanilla JS + Plotly: tabs, `<select>` and
`<input type=range>` controls, hover, legend clicks to toggle series. Proven
patterns: a budget simulator with per-channel sliders through the fitted
saturation curves (with "apply optimizer plan" and reset), a period selector
that updates the key numbers and the waterfall in place, a weekly-trends tab
with a rangeslider, and click-a-bar-to-highlight across charts and tables.

**What goes on an MMM dashboard is not a style question** — read
[dashboard/CONTENT.md](dashboard/CONTENT.md): executive narrative first; CIs on
the waterfall; spend share vs effect share PLUS the reallocation read; ROI and
CPIK with 90% CIs; marginal next to average ROI; saturation position;
min/max-rebalanced baseline; budget optimizer view. Original units everywhere;
uncertainty on anything a decision hangs on. For a dashboard that must
recompute live (sliders re-running a model), use a marimo notebook instead.

**Scheduled data refresh** — a dashboard served as a daimon artifact can update
itself without republishing: publish with `store: read`, have the page read its
numbers from the artifact store on load (`const DB = await window.daimon.use('db')`,
then `DB.collection('channels').get()` / `DB.doc('meta/latest').get()`), and keep
the embedded numbers as the fallback when the store is absent. A scheduled daimon
routine (daily or weekly) pulls the latest MMM run outputs and writes one
`channels/<name>` document per channel plus `meta/latest` (refresh timestamp,
period) — viewers get fresh numbers on next load, and the page shows the refresh
stamp. Keep the store schema flat (one doc per channel, plain numeric fields in
original units).

### Without a template, or with a requested one

Match the look by hand: Inter (or the closest humanist sans), navy text on
white, indigo links, one column, 18–19px body, rules-only tables, soft-white
code blocks, the small dark wordmark. Run the lint on the result. Say plainly in
the delivery that this is an unbranded fallback.

There is no Word or Google Docs template. For a Docs deliverable, apply the
same rules by hand, or export a PDF from `pymc-plain.typ` and attach that.

Honor an explicitly requested client or legacy template, including the older
margin-note report class. Otherwise use this style for every new artifact,
including sales collateral.

## Palette and charts

### Palette

| Token | Hex | Use |
|---|---|---|
| navy | `#0C1F40` | all text, headings, rules, the first chart series |
| indigo | `#5462C4` | the one accent: links and the prompt Copy button |
| soft-white | `#F7F7F7` | code and prompt blocks |
| white | `#FFFFFF` | page |
| teal, dark-orange | `#0C9E82`, `#C4720A` | chart series only |
| aqua, periwinkle, peach, violet | `#B4E7DD`, `#9FAAE2`, `#F6AE72`, `#C8B4E7` | chart fills and series only, never page decoration |
| navy-deep, navy-header | `#07142A`, `#071530` | slides on a dark background only |

Lighten or darken with `rgba()` alpha on a palette color, never a new hex.

### Matplotlib style

```python
import matplotlib as mpl
mpl.rc_file("mpl/matplotlibrc")     # website palette, Inter, navy titles
```

`mpl/plotstyle.py` carries `PALETTE` (fills), `PALETTE_TEXT` (text accents),
`CYCLE` (series order: strong colors first, pale last) and a `fill_between`
patch that gives clean edges; `mpl/axes.py` adds `add_axis_end_tick_caps`.
ArviZ draws through matplotlib, so `az.plot_posterior` and friends inherit the
style once the rc is loaded.

For a PDF, save **vector PDF** and embed with `image("fig.pdf")` (needs
matplotlib ≥ 3.10.1; do not switch to SVG path output, the Inter statics share
a PostScript name and SVG path-mode collides bold and regular glyphs). For an
HTML page, save PNG at 200 dpi and reference it from the Markdown.

The cycle, if you need it by hand:

```
0C1F40  navy        0C9E82  teal         F6AE72  peach        5462C4  indigo
9FAAE2  periwinkle  C4720A  dark-orange  C8B4E7  violet       B4E7DD  aqua
```

Aqua, violet and periwinkle are low-contrast lines on white; with more than
five series, prefer small multiples to the tail of the cycle.

### Chart rules that matter more than color

- **Assign series colors in the cycle's fixed order.** Never reorder by rank — a
  filter that drops a series must not repaint the survivors.
- **Never a dual y-axis.** Two measures of different scale become two charts,
  small multiples, or an index to a common base.
- **Sequential: one hue, light→dark. Diverging: two hues, neutral midpoint.**
  Never a rainbow, never a hue at a diverging midpoint.
- **A legend whenever there are 2+ series.** With ≤ 4, label directly too.
  Identity must never rest on color alone.
- **Beyond the cycle, do not invent another color.** Fold the tail into "Other",
  or use small multiples.
- **Label the axes and name the units.** The title states the finding, not the
  variable names.
- **Real data only.** No illustrative or decorative charts; a chart of
  simulated data says so in its caption.

## Non-negotiables

1. **Register the bundled fonts BEFORE any matplotlib figure** — the rc names Inter,
   but without registration charts silently fall back to DejaVuSans:
   ```python
   from pathlib import Path
   import matplotlib.font_manager as fm
   for f in Path("<this skill>/fonts").glob("*.ttf"):
       fm.fontManager.addfont(str(f))
   ```
2. **Palette hex values only — including every CSS color literal.** Any hex in a
   style block, inline style or hover state is a palette value, white, or
   navy at an `rgba()` alpha. Never hand-mix a tint or shade.
3. **Every HTML or PDF document carries the small dark wordmark** at the top
   left (the templates do this). A chart image alone does not need it.
4. **Naming a font is not loading it.** Every HTML page actually loads Inter (and
   JetBrains Mono when code appears): embedded `@font-face` (the default in
   `build_page.py`) or a Google Fonts `<link>`. The lint blocks a page that names
   Inter without loading it.
5. **Run `lint/publish_lint.py` before publishing any HTML**, and publish only
   with zero BLOCK findings.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| PDF headings come out in a serif | `font_paths` missing from `typst.compile` | pass `font_paths=["../fonts"]` and `root=".."` |
| Chart text in DejaVu Sans | fonts not registered before the first figure | non-negotiable 1 |
| Lint BLOCK `cards` / `pills` on a dashboard | old `.cards` / `.badge` markup | use `.keynums` / `.keynum` and `.status` |
| Lint BLOCK `buttons` on a page | a decorative or fake action button | delete it; only prompts get a Copy button |
| Page has a sidebar or second column at desktop width | hand-built layout | rebuild through `page/build_page.py` |
| Notebook title shows an italic word | old `accent=` argument | it is ignored now; restate the title as the point |
