// ============================================================================
//  PyMC Labs — Business Report template for Typst
//  -------------------------------------------------------------------------
//  Design language:
//    · Body / captions ......... Inter            (humanist sans, long-read)
//    · Headings / display ...... Inter             (website headline family)
//    · Mathematics ............. Fira Math         (sans-serif math)
//    · Layout .................. Tufte-style ~⅔ text column + wide margin
//                                for figures, captions and sidenotes
//    · Typography .............. follows Butterick's "Practical Typography":
//                                restrained color, comfortable measure,
//                                one idea per rule, tables without verticals.
//
//  Brand palette (pymc-labs.com, MarketingLayout.BTaXrrww.css, 2026-09-28):
//    Navy            #0C1F40   (primary / text)
//    Navy deep       #07142A   (dark surfaces, footer)
//    Navy header     #071530   (dark header band)
//    Aqua            #B4E7DD   (rules, spines, fills)
//    Periwinkle      #9FAAE2   (secondary fills, series)
//    Peach           #F6AE72   (warm fill — use sparingly)
//    Violet          #C8B4E7   (optional fill)
//    Soft white      #F7F7F7   (surfaces)
//  Text accents (readable on white / soft white; the pale colors are not):
//    Teal            #0C9E82   (accent text, icons; pairs with an aqua tint)
//    Indigo          #5462C4   (link / interactive accent text)
//    Dark orange     #C4720A   (warm status text; pairs with a peach tint)
// ============================================================================

#import "@preview/marginalia:0.2.0" as marginalia

// ---------------------------------------------------------------------------
//  Palette
// ---------------------------------------------------------------------------
#let navy       = rgb("#0C1F40")
#let aqua       = rgb("#B4E7DD")
#let periwinkle = rgb("#9FAAE2")
#let soft-white = rgb("#F7F7F7")
#let peach      = rgb("#F6AE72")
#let navy-deep   = rgb("#07142A")
#let navy-header = rgb("#071530")
#let violet      = rgb("#C8B4E7")
#let teal        = rgb("#0C9E82")   // text accents: readable on white
#let indigo      = rgb("#5462C4")
#let dark-orange = rgb("#C4720A")
#let ink        = navy                       // website text color
#let muted      = ink
#let hairline   = navy.lighten(78%)          // faint structural rules (not text)

// ---------------------------------------------------------------------------
//  Fonts
// ---------------------------------------------------------------------------
#let body-font    = "Inter"        // bundled rsms static; registers as family "Inter"
#let heading-font = "Inter"        // website headline family
#let math-font    = "Fira Math"    // sans math
#let mono-font    = "JetBrains Mono"
#let heading-stretch = 100%
#let heading-tracking = -0.02em   // website h1 letter-spacing

// small helper: content -> string (best-effort, for document metadata)
#let to-string(content) = {
  if type(content) == str { content }
  else if content == none { "" }
  else if content.has("text") { content.text }
  else if content.has("children") { content.children.map(to-string).join("") }
  else if content.has("body") { to-string(content.body) }
  else { "" }
}

// Body font size, published by `pymc-report` so the margin helpers (sidenotes,
// captions, key figures) can scale proportionally with it. Margin text is set
// to ≈0.81× the body size (8.5pt at the 10.5pt default).
#let _fs = state("pymc-fontsize", 10.5pt)
#let _margin-size = 0.81

// Page geometry matching Tufte-LaTeX exactly, per paper size.
//   left/right = outer page margins, gutter = text↔margin separation,
//   mcol = margin (sidenote) column width.
// Letter: left 1in, text 26pc, sep 2pc, marginpar 12pc (tufte-common.def).
// A4: left 24.8mm, text 107mm, sep 8.2mm, marginpar 49.4mm (tufte-common.def).
#let _tufte-geom(paper) = if paper == "us-letter" {
  (left: 25.4mm, right: 21.2mm, mcol: 50.8mm, gutter: 8.47mm, top: 25.4mm, bottom: 28mm)
} else {
  (left: 24.8mm, right: 20.6mm, mcol: 49.4mm, gutter: 8.2mm, top: 27.4mm, bottom: 22mm)
}
// published by `pymc-report` so full-page figures can reuse the report margins
#let _geom = state("pymc-geom", _tufte-geom("a4"))
// published by `pymc-report` so `figtag` can gate on draft mode
#let _draft = state("pymc-draft", false)

// Figure tag: a stable three-word handle for a figure, rendered in monospace at
// the end of a caption ONLY in draft mode (draft: true). It lets you point at
// "the [brisk-otter-lamp] figure" while iterating and vanishes in the final
// (draft: false) build. Usage: `caption: [ … #figtag("brisk-otter-lamp")]`.
#let figtag(code) = context if _draft.get() {
  // box() keeps the code on one line (no mid-word hyphenation in the margin)
  [#h(0.35em)#box(text(font: mono-font, fill: dark-orange)[#("[" + code + "]")])]
}

// ---------------------------------------------------------------------------
//  Brand petal / clover mark (drawn — no asset needed)
//  Four overlapping translucent discs echo the brand's "many scenarios" motif.
// ---------------------------------------------------------------------------
#let petal-mark(size: 42mm, fill: aqua, strength: 22%) = {
  let c = fill.transparentize(100% - strength)
  let r = size / 4
  box(width: size, height: size)[
    #place(center + horizon, dy: -r, circle(radius: r, fill: c, stroke: none))
    #place(center + horizon, dy: r,  circle(radius: r, fill: c, stroke: none))
    #place(center + horizon, dx: -r, circle(radius: r, fill: c, stroke: none))
    #place(center + horizon, dx: r,  circle(radius: r, fill: c, stroke: none))
  ]
}

// ---------------------------------------------------------------------------
//  Margin helpers (exposed to authors)
// ---------------------------------------------------------------------------

// Superscript numeral marker in brand color (replaces marginalia's default,
// which is hardcoded to the "Inter" family we don't ship as a static face).
#let _note-marker(..n) = super(
  box(fill: white, inset: (x: 1.5pt, y: 1pt))[
    #text(font: body-font, weight: "medium", fill: periwinkle)[#numbering("1", ..n.pos())]
  ]
)

// Caption formatter: bold "Figure N." / "Table N." lead, body in `fill`.
// Size is inherited from the surrounding context (margin note or page block).
#let _capfmt(it, fill: muted) = {
  set text(fill: fill)
  set par(justify: false)       // left-aligned (ragged-right), last line left
  set align(left)
  strong[#it.supplement #context it.counter.display(it.numbering)#it.separator]
  it.body
}

// Numbered sidenote — the Tufte replacement for footnotes (citations, asides).
// Our own counter so the inline reference (periwinkle superscript) and the
// margin number (a white chip sitting ON the periwinkle rule) stay in sync.
#let _sncount = counter("pymc-sidenote")
#let sidenote(body, ..args) = {
  _sncount.step()
  context {
    let n = _sncount.get().at(0)
    let msize = _margin-size * _fs.get()
    // inline reference: scaled regular figure (typographic:false) so it matches
    // the margin number exactly (same glyph, size and weight)
    super(typographic: false, text(font: body-font, weight: "bold", fill: periwinkle)[#n])
    marginalia.note(
      numbering: none, keep-order: true,
      text-style: (size: msize, fill: ink, style: "normal", weight: "light"),
      ..args,
    )[
      // Full-height periwinkle rule; the number is overlaid at the top, its right
      // edge flush with the rule's right edge (tucked into the gutter), on a white
      // chip so the rule reads as starting below it. Number at body size.
      #block(stroke: (left: 1.5pt + periwinkle), inset: (left: 5pt, top: 0pt, bottom: 3pt))[
        #place(top + left, dx: -20pt, dy: 0pt,
          box(width: 16pt, fill: white, inset: (top: 0pt, bottom: 3.5pt),
            align(right, text(size: 0.6 * _fs.get(), weight: "bold", fill: periwinkle)[#n])))
        #body
      ]
    ]
  }
}

// Un-numbered margin commentary (assumptions, caveats, definitions).
#let marginnote(body, ..args) = context marginalia.note(
  numbering: none,
  text-style: (size: _margin-size * _fs.get(), fill: ink, style: "normal", weight: "light"),
  block-style: (width: 100%, stroke: (left: 1.5pt + periwinkle),
                inset: (left: 8pt, top: 3pt, bottom: 4pt)),
  ..args, body,
)

// A figure that lives entirely in the margin (small plots), caption beneath.
// Built on marginalia.note (not notefigure) so we control the caption directly:
// same size as other margin captions, with the bold "Figure N." lead.
#let marginfigure(content, caption: none, label: none, kind: auto, supplement: auto) = context {
  let fs = _fs.get()
  show figure.caption: it => _capfmt(it, fill: ink)
  marginalia.note(
    numbering: none, keep-order: true, dy: 0pt, align-baseline: false,
    text-style: (size: _margin-size * fs, style: "normal", weight: "light"),
    par-style: (leading: 0.5em, spacing: 0.5em, hanging-indent: 0pt),
  )[
    #figure(content, caption: caption, kind: kind, supplement: supplement, gap: 0.85em) #label
  ]
}

// A table with Butterick / booktabs-style rules: heavier top & bottom, a light
// rule under the header row. Use exactly like the built-in `table(...)`.
#let dtable(..args) = table(
  table.hline(y: 0, stroke: 0.9pt + navy),
  table.hline(y: 1, stroke: 0.5pt + hairline),
  ..args,
  table.hline(stroke: 0.9pt + navy),
)

// A figure kept in the text column, with its caption pulled into the right
// margin (Tufte style — used for tables and larger plots). The caption is
// lifted to align with the *top* of the figure. Referenceable via the
// `label:` argument, e.g. `flowfigure(dtable(...), caption: [...], label: <t1>)`.
#let flowfigure(body, caption: none, label: none, kind: auto, supplement: auto) = block(
  // breathing room above and below the figure. This block MUST wrap the layout
  // (not sit inside it): Typst discards weak spacing at a container's edges, so
  // an above/below set inside layout() is silently dropped.
  above: 1.9em, below: 1.9em,
  layout(size => {
    // measure the body at the text-column width so we can raise the caption to
    // sit beside the top of the figure rather than its bottom.
    let h = measure(block(width: size.width, body)).height
    let cap-size = _margin-size * _fs.get()
    show figure.caption: cap => marginalia.note(
      numbering: none, keep-order: true, dy: -(h + 0.6em), align-baseline: false,
      text-style: (size: cap-size, fill: muted, style: "normal", weight: "light"),
      par-style: (leading: 0.5em, spacing: 0.5em, hanging-indent: 0pt),
    )[#_capfmt(cap, fill: ink)]
    [#figure(body, caption: caption, kind: kind, supplement: supplement, gap: 0.6em) #label]
  }),
)

// A block that spans the full width (text + margin) — for wide tables/plots.
#let wideblock = marginalia.wideblock

// A figure spanning the full content width (text + margin column). The caption
// goes in the margin, anchored just below the figure (a full-width body leaves
// no room beside it). Referenceable via `label:`.
#let widefigure(body, caption: none, label: none, kind: auto, supplement: auto) = block(
  above: 1.9em, below: 1.9em,
  context {
    let cap-size = _margin-size * _fs.get()
    show figure.caption: cap => marginalia.note(
      numbering: none, keep-order: true, dy: 0.4em,
      text-style: (size: cap-size, fill: muted, style: "normal", weight: "light"),
      par-style: (leading: 0.5em, spacing: 0.5em, hanging-indent: 0pt),
    )[#_capfmt(cap, fill: ink)]
    wideblock(side: "both")[
      #figure(body, caption: caption, kind: kind, supplement: supplement, gap: 0.6em) #label
    ]
  },
)

// A figure on its own dedicated page: the body fills the page, vertically
// centered, with a full-width caption beneath. Use for large diagrams/plots.
// Referenceable via `label:`. (For a placeholder body, pass `kind: image`.)
#let fullpagefigure(body, caption: none, label: none, kind: auto, supplement: auto) = context {
  let g = _geom.get()   // reuse the report's page margins
  page(margin: (left: g.left, right: g.right, top: g.top, bottom: g.bottom))[
    // full-page captions are primary reading: navy, bold lead, slightly larger.
    #show figure.caption: it => context block(width: 100%, {
      set text(size: 1.05 * _fs.get(), fill: ink)
      set par(justify: false, leading: 0.6em)
      set align(left)
      _capfmt(it, fill: ink)
    })
    #set figure(gap: 1.1em)
    #v(1fr)
    #align(center)[#figure(body, caption: caption, kind: kind, supplement: supplement) #label]
    #v(1fr)
  ]
}

// A "key number" pulled into the margin: large Inter value + tight label.
// Both scale with the body font size (≈20pt / 8pt at the 10.5pt default).
#let keyfigure(value, label) = context {
  let fs = _fs.get()
  marginalia.note(
    numbering: none,
    text-style: (size: _margin-size * fs, fill: ink, style: "normal", weight: "light"),
    par-style: (leading: 0.3em, spacing: 0.3em, hanging-indent: 0pt),
  )[
    #text(font: heading-font, stretch: heading-stretch, weight: "medium",
          size: 1.9 * fs, fill: ink)[#value]
    #v(0.33 * fs, weak: false)
    // pad the whole label (not just the first line) so every wrapped line stays
    // indented, aligned with the first word
    #pad(left: 1.0 * fs, text(size: 0.78 * fs, fill: ink)[#upper(label)])
  ]
}

// ---------------------------------------------------------------------------
//  In-flow components
// ---------------------------------------------------------------------------

// Width of the colored left spine on the panel boxes. Typst centers a stroke on
// the border, so half of it spills into the left margin; we shift each box right
// by `_spine-w/2` (a `pad`) so the spine's LEFT edge sits flush on the text
// margin. The outer block carries the above/below spacing so it is not dropped.
#let _spine-w = 2.5pt

// Executive summary — a quiet panel with an aqua spine.
#let executive-summary(title: "Executive summary", body) = block(above: 0.4em, below: 4.0em,
  pad(left: _spine-w / 2, block(
    width: 100%, fill: soft-white, inset: (x: 16pt, y: 14pt),
    stroke: (left: _spine-w + aqua), radius: 2pt, breakable: false,
  )[
    // title: a headline, but smaller than a section heading
    #context text(font: heading-font, stretch: heading-stretch, weight: "medium",
          size: 1.3 * _fs.get(), fill: ink)[#title]
    #v(0.6em)
    #set par(first-line-indent: 0pt, justify: false)
    #body
  ]))

// Inline callout for a result worth emphasizing (use rarely).
// Consistent with the executive-summary block: soft-white fill + aqua left bar.
#let callout(body, accent: aqua) = block(above: 1.8em, below: 1.8em,
  pad(left: _spine-w / 2, block(
    width: 100%, fill: soft-white, inset: 12pt,
    stroke: (left: _spine-w + accent), radius: 2pt, breakable: false,
  )[
    #set par(first-line-indent: 0pt)
    #body
  ]))

// Quote / pull-quote box — transparent background, colored left spine (like the
// callout but no fill), italic body so it pops out. `accent` defaults to
// periwinkle; pass any palette color (navy, aqua, peach, …). Optional `by:` adds
// an attribution line below, right-aligned, smaller and gray with a leading em
// dash; omit it and the box renders on its own.
#let quotebox(body, by: none, accent: periwinkle) = {
  // The spine is a FLAT (square) breakable stroke, so when the box splits across
  // pages the break edges are flat (the parts read as one box). Rounded end-caps
  // are placed at the content's true top and bottom, so they land only on the
  // first and last fragments; middle fragments stay flat on both ends. (Typst
  // can't vary `radius` per broken fragment — issue #2176 — hence the manual caps.
  // Each cap erases the square spine end to white, then redraws it as a STROKED
  // rounded corner using the SAME construction as the callout/exec-summary boxes
  // (left stroke of width _spine-w, radius _spine-w-matching `cr`), so the
  // curvature/length matches those boxes exactly. This assumes a white page behind
  // the spine.)
  let il = 14pt                          // left inset (quote text indent)
  let cr = 2pt                           // corner radius (matches the callout box)
  let cdx = -(_spine-w / 2 + il)         // from content origin to the spine's left edge
  let boxed = pad(left: _spine-w / 2, block(
    width: 100%, inset: (left: il, top: 2pt, bottom: 2pt),
    stroke: (left: _spine-w + accent), radius: 0pt,
  )[
    // `dx: -il` lands the cap block's left border on the spine's (= pad offset),
    // so its centered stroke coincides with the flat spine's.
    #place(top + left, dx: cdx, dy: -3pt, rect(width: 3pt, height: 3pt, fill: white))
    #place(top + left, dx: -il, dy: -2pt,
           block(width: 8pt, height: 8pt, stroke: (left: _spine-w + accent), radius: (top-left: cr)))
    #place(bottom + left, dx: cdx, dy: 3pt, rect(width: 3pt, height: 3pt, fill: white))
    #place(bottom + left, dx: -il, dy: 2pt,
           block(width: 8pt, height: 8pt, stroke: (left: _spine-w + accent), radius: (bottom-left: cr)))
    #set par(first-line-indent: 0pt)
    #set text(style: "italic")
    #body
  ])
  if by == none {
    block(above: 1.8em, below: 1.8em, boxed)
  } else {
    block(above: 1.8em, below: 1.8em)[
      #boxed
      #v(0.1em, weak: false)
      #align(right, text(size: 0.88em, fill: luma(40%))[#sym.dash.em #by])
    ]
  }
}

// Appendix divider — unnumbered. The first appendix starts a fresh page; later
// ones just follow in the flow (the heading's own top spacing separates them).
#let _appendix-count = counter("pymc-appendix")
#let appendix(letter, title) = {
  _appendix-count.step()
  context if _appendix-count.get().at(0) == 1 { pagebreak(weak: true) }
  heading(level: 1, outlined: true)[
    Appendix #letter: #title
  ]
}

// ---------------------------------------------------------------------------
//  The cover  (style: "subtle brand accent")
// ---------------------------------------------------------------------------
#let cover(
  title: none, subtitle: none, client: none, date: none,
  author: none, status: "Confidential", paper: "a4",
  logo: "../assets/pymc-labs-logo-dark.png",
  cover-background: none,              // plain by default, or a custom image path
  draft: false,                    // true → a bold DRAFT mark in the top-right
) = {
  let g = _tufte-geom(paper)
  let _bg = cover-background
  // Cover uses its own tighter margin (sleeker, content closer to the edges)
  // than the Tufte body pages.
  let cm = 12.75mm
  page(paper: paper, margin: (left: cm, right: cm, top: cm, bottom: cm * 1.5),
       header: none, footer: none,
       background: if _bg != none {
         image(_bg, width: 100%, height: 100%, fit: "cover")
       })[
    // — masthead: logo top-left, document metadata top-right —
    #grid(
      columns: (1fr, auto),
      align: (left + top, right + top),
      image(logo, height: 14mm),
      {
        set align(right)
        if draft {
          text(font: body-font, fill: dark-orange, weight: "bold",
               size: 16pt, tracking: 0.08em)[DRAFT]
          v(8pt)
        }
        text(font: body-font, size: 9.5pt, fill: navy)[
          #set par(leading: 0.55em, justify: false)
          #if client != none [Prepared for #client \ ]
          #if date != none [#date \ ]
          #if status != none [#text(fill: dark-orange)[#status]]
        ]
      },
    )

    #v(1fr)

    // — title + byline, lower-left, in deep navy —
    #block(width: 86%)[
      #set par(justify: false, first-line-indent: 0pt)
      #set text(hyphenate: false)
      #text(font: heading-font, stretch: heading-stretch, weight: "semibold",
            tracking: heading-tracking, size: 46pt, fill: navy)[
        #set par(leading: 0.32em)
        #title
      ]
      #if subtitle != none [
        // byline: narrower than the title (80% of the block) with an explicit
        // gap above it (controls the title↔byline distance)
        #block(width: 80%, above: 23.1pt)[
          #set par(leading: 0.5em)
          #text(font: body-font, size: 16pt, fill: navy)[#subtitle]
        ]
      ]
    ]
  ]
}

// ---------------------------------------------------------------------------
//  The template entry point
// ---------------------------------------------------------------------------
#let pymc-report(
  title: none,
  subtitle: none,
  client: none,
  author: none,
  date: none,
  status: "Confidential",
  paper: "a4",                      // "a4" or "us-letter"
  abstract: none,                   // optional executive-summary content
  outline-depth: 2,                 // 0 disables the table of contents
  number-equations: true,
  number-headings: false,           // true → "1.1" section numbers (long reports)
  font-size: 9.5pt,                 // body text size; headings scale with it
  logo: "../assets/pymc-labs-logo-dark.png",
  cover-background: none,               // none for plain; a path for supplied artwork
  draft: false,                     // true → DRAFT mark on the cover + footer
  highlight-code: false,            // true → brand syntax colors in code blocks
  body,
) = {
  // — document metadata —
  set document(
    title: if title != none { to-string(title) } else { "PyMC Labs report" },
    author: if author != none { to-string(author) } else { "PyMC Labs" },
  )

  // — page size (applies to the body; marginalia.setup only sets margins, and
  //   the cover sets its own paper, so this is what keeps body pages on-paper) —
  set page(paper: paper)
  let g = _tufte-geom(paper)

  // publish the body size + geometry so margin helpers / full-page figures match
  _fs.update(font-size)
  _geom.update(g)
  _draft.update(draft)

  // — global text & paragraph —
  set text(font: body-font, size: font-size, fill: ink, lang: "en",
           weight: "regular", hyphenate: true, fallback: true)
  // Semibold emphasis against regular body text.
  show strong: it => text(weight: "semibold", it.body)
  // Tufte-style: left-aligned (ragged-right) in both columns — lighter, and
  // avoids justification gaps/rivers in the narrow margin.
  // Block paragraphs: no first-line indent, a small gap between paragraphs.
  set par(justify: false, leading: 0.72em, spacing: 1.05em,
          first-line-indent: 0pt)

  // — lists: room between items and around the whole list —
  set list(spacing: 0.95em, indent: 1.1em, marker: text(fill: periwinkle)[•])
  set enum(spacing: 0.95em, indent: 1.1em)
  set terms(spacing: 0.95em, indent: 0pt, hanging-indent: 1.3em)
  show list: set block(above: 1.3em, below: 1.3em)
  show enum: set block(above: 1.3em, below: 1.3em)
  show terms: set block(above: 1.3em, below: 1.3em)

  // — mathematics in a sans face, with room to breathe —
  // Fira Math now ships a complete Light (300) cut (rebuilt locally); pin it
  // explicitly so the math weight tracks the Light body rather than inheriting.
  show math.equation: set text(font: math-font, weight: "light")
  set math.equation(numbering: if number-equations { "(1)" } else { none })
  show math.equation.where(block: true): set block(above: 1.45em, below: 1.45em)

  // — headings (unnumbered, typographic hierarchy only) —
  set heading(numbering: if number-headings { "1.1" } else { none },
              supplement: [Section])
  show heading: set text(font: heading-font, fill: ink)
  show heading.where(level: 1): it => block(above: 3.0em, below: 1.9em)[
    #set text(stretch: heading-stretch, weight: "semibold", tracking: heading-tracking,
              size: 1.70 * font-size)
    #if it.numbering != none [#counter(heading).display(it.numbering)#h(0.55em)]#it.body
  ]
  show heading.where(level: 2): it => block(above: 1.7em, below: 1.2em)[
    #set text(stretch: heading-stretch, weight: "medium", tracking: -0.015em,
              size: 1.33 * font-size)
    #if it.numbering != none [#counter(heading).display(it.numbering)#h(0.5em)]#it.body
  ]
  show heading.where(level: 3): it => block(above: 1.4em, below: 0.85em)[
    #set text(font: body-font, weight: "regular", size: 1.12 * font-size, fill: ink)
    #if it.numbering != none [#counter(heading).display(it.numbering)#h(0.5em)]#it.body
  ]

  // — links: distinguishable but quiet —
  // underline only real (URL) links; internal links (TOC, cross-refs) stay plain
  show link: it => if type(it.dest) == str {
    underline(offset: 2pt, stroke: 0.5pt + periwinkle, it)
  } else { it }

  // — tables: Butterick style — horizontal rules only, no verticals —
  set table(stroke: none, inset: (x: 8pt, y: 5pt))
  // header row: same body font as the rest, just bold
  show table.cell.where(y: 0): set text(weight: "medium")

  // — raw / code (JetBrains Mono on soft-white surfaces) —
  // Monochrome navy by default (restrained); with highlight-code, code blocks
  // take the brand syntax theme (lib/brand-code.tmTheme). Themed token colors
  // are explicit and override the ambient navy; untokenised text stays navy.
  set raw(theme: if highlight-code { "brand-code.tmTheme" } else { none })
  show raw: set text(font: mono-font, size: 1.0em, fill: navy)
  // inline code: a faint soft-white chip so it reads as a token in running text.
  // Render the literal text so inline code stays monochrome navy even when
  // highlight-code themes the blocks.
  show raw.where(block: false): it => box(
    fill: soft-white, inset: (x: 3pt), outset: (y: 3pt), radius: 2pt,
    text(font: mono-font, fill: navy, it.text),
  )
  // code blocks: a soft-white slab. A soft-wrapped logical line indents its
  // continuation by one char and gets a thin gray bracket — a vertical rule from
  // the first character down the wrapped lines, with a small corner to the right
  // at the bottom. The indent is paragraph hanging-indent and the bracket is a
  // drawn line(), so neither becomes characters when the code is copied.
  // (JetBrains Mono advance ≈ 0.6em per char.)
  show raw.where(block: true): it => block(
    fill: soft-white, width: 100%, radius: 0pt,
    inset: (x: 11pt, y: 9pt), above: 2.0em, below: 2.0em,
    layout(size => {
      let ch = 0.6em                              // monospace advance
      let sc = 0.5pt + rgb("#b9bcc4")             // very thin gray
      let rows = it.lines.map(ln => {
        let lead = ln.text.len() - ln.text.trim(" ", at: start).len()
        let p = {
          set par(leading: 0.55em, hanging-indent: (lead + 1) * ch)
          ln.body
        }
        // only bracket lines that actually overflow the column (soft-wrap)
        if measure(ln.body).width <= size.width { p } else {
          let h = measure(box(width: size.width, p)).height
          let x = lead * ch - 0.15em              // just left of the first character
          let ytop = 0.35em                       // starts on the leading line
          let ybot = h - 0.4em                    // ends on the last wrapped line
          box(width: size.width, height: h, {
            place(top + left, dx: x, dy: ytop,
                  line(end: (0pt, ybot - ytop), stroke: sc))   // vertical rule
            place(top + left, dx: x, dy: ybot,
                  line(end: (0.6 * ch, 0pt), stroke: sc))      // corner to the right
            p
          })
        }
      })
      // round only the box's true top/bottom (first/last fragment), full width,
      // so a code block spanning pages has flat break edges and reads as one box
      let capw = size.width + 22pt              // box width = content + 2*11pt inset
      let cr = 3pt
      place(top + left, dx: -11pt, dy: -9pt, rect(width: capw, height: cr, fill: white))
      place(top + left, dx: -11pt, dy: -9pt, rect(width: capw, height: cr, fill: soft-white, radius: (top: cr)))
      place(bottom + left, dx: -11pt, dy: 9pt, rect(width: capw, height: cr, fill: white))
      place(bottom + left, dx: -11pt, dy: 9pt, rect(width: capw, height: cr, fill: soft-white, radius: (bottom: cr)))
      stack(dir: ttb, spacing: 0.55em, ..rows)
    }),
  )

  // ===== COVER =====
  cover(
    title: title, subtitle: subtitle, client: client, date: date,
    author: author, status: status, paper: paper, logo: logo,
    cover-background: cover-background, draft: draft,
  )

  // ===== BODY (Tufte margin layout) =====
  show: marginalia.setup.with(
    inner: (far: g.left, width: 0mm, sep: 0mm),
    outer: (far: g.right, width: g.mcol, sep: g.gutter),
    top: g.top, bottom: g.bottom, clearance: 16pt,
  )

  // running footer (cover already rendered without one)
  set page(footer: context {
    line(length: 100%, stroke: 0.6pt + ink)
    v(4pt)
    grid(columns: (1fr, auto, 1fr), align: (left, center, right),
      text(font: body-font, size: 8pt, fill: ink)[
        PyMC Labs#if status != none [ · #text(fill: dark-orange)[#status]]
      ],
      if draft { text(font: body-font, size: 8pt, fill: dark-orange)[\[DRAFT\]] },
      text(font: heading-font, size: 8pt, fill: ink)[
        #counter(page).display("1")
      ],
    )
  })

  // Expose the code column width (in monospace characters) as queryable metadata
  // so the external formatter tool (tools/wrap_typ_code.py) can wrap ```python
  // blocks to the exact column. Zero-size / invisible. Read with:
  //   typst query FILE.typ "<pymc-code-cols>" --field value --one
  // Width = page − page margins − margin column − gutter − code inset (2×11pt),
  // divided by the measured JetBrains Mono advance at the body/code size.
  context {
    let pw = if paper == "us-letter" { 8.5in } else { 210mm }
    let usable = pw - g.left - g.right - g.mcol - g.gutter - 22pt
    // Measure the ACTUAL rendered code advance: `raw` carries the real code
    // text styling, which renders narrower than a plain `text` at the body size
    // (its `1em` resolves smaller). Differencing two lengths cancels the inline
    // code chip's fixed inset, leaving the pure per-character advance.
    let adv = (measure(raw("m" * 60, lang: none)).width
               - measure(raw("m" * 20, lang: none)).width) / 40
    [#metadata(calc.floor(usable / adv)) <pymc-code-cols>]
  }

  // executive summary
  if abstract != none {
    executive-summary(abstract)
  }

  // table of contents — single font (Inter), no dot leaders, airy spacing
  if outline-depth > 0 {
    // "Contents" title styled like a section heading (no number), space below
    block(above: 3.0em, below: 1.5em)[
      #text(font: heading-font, stretch: heading-stretch, weight: "semibold",
            tracking: heading-tracking, size: 1.70 * font-size, fill: ink)[Contents]
    ]
    show outline.entry: it => {
      set text(font: body-font, fill: ink, weight: "light")
      block(above: 0.75em, below: 0pt,
        link(it.element.location(),
          it.indented(it.prefix(), it.body() + h(1fr) + it.page())))
    }
    outline(title: none, depth: outline-depth)
  }

  body
}
