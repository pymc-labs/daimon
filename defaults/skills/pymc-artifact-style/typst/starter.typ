// Minimal PyMC Labs report — verified to compile with the bundled fonts/logo.
// Copy this, replace the content, keep the structure.
// Build:  python -c "import typst; typst.compile('starter.typ', output='out.pdf', root='..', font_paths=['../fonts'])"
#import "pymc-report.typ": *

#show: pymc-report.with(
  title:    [Report title],
  subtitle: [One-line description of the work],
  client:   [Client name],
  author:   [PyMC Labs],
  date:     [August 2026],
  status:   "Confidential",
  paper:    "a4",
  cover-background: none,
  abstract: [
    Two or three sentences a stakeholder can read on its own: what was asked,
    what was found, what to do about it.
  ],
  outline-depth: 2,
)

= Background

Body text sits in the ⅔ column. Sourcing goes in the margin as a numbered
sidenote#sidenote[Author, _Title_, 2026.], not a footnote at the page foot.

#marginnote[Un-numbered margin commentary — caveats and assumptions belong
here, beside the prose they qualify.]

= Results

#keyfigure[1.21x][Best estimated ROAS]

#flowfigure(
  dtable(
    columns: 3,
    table.header([Channel], [Coefficient], [94% HDI]),
    [TV],     [0.188], [[0.010, 0.335]],
    [Search], [0.243], [[0.067, 0.425]],
    [Social], [0.204], [[0.024, 0.363]],
  ),
  caption: [Posterior summaries. Horizontal rules only — no verticals.],
  label: <tbl-channels>,
)

As @tbl-channels shows, all three intervals exclude zero.

#callout[
  A single emphasis box. Use sparingly — color earns its place.
]

= Recommendation

#quotebox(by: [PyMC Labs])[
  State the decision, not the analysis.
]
