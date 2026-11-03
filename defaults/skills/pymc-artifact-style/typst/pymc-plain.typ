// PyMC Labs plain document class: the default for reports, briefs and one-pagers.
// One column, full width, large text, small wordmark, no cover, no margin notes.
//
//   #import "pymc-plain.typ": *
//   #show: pymc-plain.with(title: [A title that states the point], lede: [The answer in two sentences.])
//
// Build (font_paths is REQUIRED, or headings silently fall back to a serif):
//   python -c "import typst; typst.compile('starter.typ', output='out.pdf', root='..', font_paths=['../fonts'])"

#let navy   = rgb("#0C1F40")
#let indigo = rgb("#5462C4")   // the one accent: links only
#let muted  = navy.lighten(28%)
#let rule-c = navy.lighten(80%)
#let code-bg = rgb("#F7F7F7")

// Rules-only table: heavy top and bottom, light header rule, no verticals, no fills.
#let dtable(..args) = table(
  stroke: (x, y) => if y == 0 { (bottom: 0.6pt + navy) },
  inset: (left: 0pt, right: 18pt, y: 6pt),
  ..args.named(),
  table.hline(stroke: 1.4pt + navy),
  ..args.pos(),
  table.hline(stroke: 1.4pt + navy),
)

#let pymc-plain(
  title: none,
  lede: none,                    // first paragraph: the answer, set larger
  date: none,                    // e.g. [November 2026]; goes in the footer line
  footer-note: none,             // e.g. [Example data, not a real business.]
  paper: "a4",                   // or "us-letter"
  font-size: 14pt,               // large on purpose; do not go below 12pt
  logo: "../assets/pymc-labs-logo-dark.png",
  body,
) = {
  set document(title: if title != none { title } else { "PyMC Labs" })
  set page(
    paper: paper,
    margin: (x: 22mm, top: 20mm, bottom: 22mm),
    footer: context {
      set text(size: 0.72 * font-size, fill: muted)
      [PyMC Labs]
      if date != none [, #date]
      if footer-note != none [. #footer-note]
      h(1fr)
      counter(page).display()
    },
  )
  set text(font: "Inter", size: font-size, fill: navy, lang: "en")
  set par(leading: 0.75em, spacing: 1.1em, justify: false)
  show raw: set text(font: "JetBrains Mono", size: 0.85em)
  show raw.where(block: true): it => block(fill: code-bg, inset: 10pt, width: 100%, stroke: 0.5pt + rule-c, it)
  show link: set text(fill: indigo)
  set heading(numbering: none)
  show heading: set text(weight: 600, tracking: -0.015em)
  show heading.where(level: 1): set text(size: 1.55 * font-size)
  show heading.where(level: 1): set block(above: 1.8em, below: 0.6em)
  show heading.where(level: 2): set text(size: 1.2 * font-size)
  show heading.where(level: 2): set block(above: 1.5em, below: 0.5em)
  show figure.caption: set text(size: 0.85 * font-size, fill: muted)
  set figure(gap: 0.6em)
  show figure: set block(above: 1.2em, below: 1.4em)
  show figure: set align(left)
  show figure.where(kind: table): set figure.caption(position: bottom)
  show table.cell.where(y: 0): set text(weight: 600)
  set list(indent: 0.4em, spacing: 0.7em)
  set enum(indent: 0.4em, spacing: 0.7em)

  image(logo, height: 6mm)
  v(8mm)
  if title != none {
    block(below: 1.0em, text(size: 2.1 * font-size, weight: 600, tracking: -0.02em, title))
  }
  if lede != none {
    block(below: 1.4em, text(size: 1.15 * font-size, lede))
  }
  body
}
