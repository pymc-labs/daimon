// Minimal PyMC Labs document: copy this, replace the content, keep the structure.
// Build: python -c "import typst; typst.compile('starter.typ', output='out.pdf', root='..', font_paths=['../fonts'])"
#import "pymc-plain.typ": *

#show: pymc-plain.with(
  title: [A title that states the finding],
  lede: [Two sentences a busy reader can stop after: what was found and what to do about it.],
  date: [November 2026],
  footer-note: none,          // e.g. [Example data, not a real business.]
)

= A heading that states the point of this section

Body text in one full-width column. Short paragraphs, sparse bullets, each number
with its unit, comparison and interval.

#figure(
  dtable(
    columns: 3,
    align: (left, right, right),
    table.header([Channel], [Coefficient], [94% interval]),
    [TV],     [0.19], [0.01 to 0.34],
    [Search], [0.24], [0.07 to 0.43],
    [Social], [0.20], [0.02 to 0.36],
  ),
  caption: [Posterior summaries from the fitted model.],
)

= What we recommend

State the decision and what it depends on. Caveats go in a plain sentence here,
where they apply, not in a box.
