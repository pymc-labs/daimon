# Website identity

Source: https://www.pymc-labs.com/, fetched 2026-09-28: the homepage HTML (inline
styles) and its one stylesheet,
https://www.pymc-labs.com/_astro/MarketingLayout.BTaXrrww.css.
The hashed stylesheet name changes on every site deploy; it replaced
`MarketingLayout.BlM-pfUk.css` (inspected 2026-09-14). Consult the homepage's
linked stylesheet when checking future changes.

## Color tokens (CSS custom properties)

`--color-navy #0C1F40`, `--color-navy-deep #07142A`, `--navy-header #071530`,
`--color-aqua #B4E7DD`, `--color-periwinkle #9FAAE2`, `--color-peach #F6AE72`,
`--color-violet #C8B4E7`, `--color-soft-white #F7F7F7`, `--color-white #FFF`.
Body text is `var(--color-navy)`; the footer background is navy-deep. Borders on
light surfaces are navy at 8% (`#0c1f4014`); on dark surfaces, white at 10%.

## Text accents (used as arbitrary Tailwind values and inline styles)

- teal `#0C9E82`: 67 occurrences in the homepage HTML, plus `text-[#0C9E82]` and
  `accent-[#0C9E82]` in the CSS. Accent text, icons and links, usually on an
  aqua tint (`rgba(180,231,221,0.3–0.4)`).
- indigo `#5462C4`: link and heading hover color (`hover:text-[#5462C4]`);
  `#3a4fc4` is a second hover shade.
- dark orange `#C4720A`: badge text on a peach tint (`rgba(246,174,114,0.3–0.35)`).

The pale colors are fills. Readable colored text uses these three.

## Type

`--font-body` and `--font-headline` are `"Inter", system-ui, sans-serif`;
`--font-mono` is `"JetBrains Mono"`; `--font-serif` is `Georgia, serif`, used
only by `.serif-accent` (italic, weight 500). Google Fonts loads Inter
300–800 and JetBrains Mono 400–600. Headline rules: h1 weight 600,
letter-spacing -.02em, line-height 1.05; h2 weight 500, letter-spacing -.015em;
h3 weight 500. Body weight 400, line-height 1.55.

## Logos

The site serves the dark and light wordmarks from Cloudinary (same versions as
the bundled PNGs, downloaded through the `f_png` transform without redrawing or
recoloring):

- Dark: https://res.cloudinary.com/dx3t8udaw/image/upload/v1787796517/website/pymc_labs_logo_dark.webp
- Light: https://res.cloudinary.com/dx3t8udaw/image/upload/v1787796594/website/pymc_labs_logo_light.webp

## Bundled fonts

Inter 4.1 statics (Light, Regular, Italic, Medium, SemiBold, Bold) from
rsms/inter, license in `../fonts/Inter-OFL.txt`. JetBrains Mono Regular and
Medium from the Google Fonts stylesheet the site requests, license in
`../fonts/JetBrainsMono-OFL.txt`. Georgia is not bundled (not freely
redistributable); it applies only to HTML.

## Document conventions (not website tokens)

The report's Tufte margin layout, Fira Math, and the plain cover are document
conventions. For HTML and notebooks, apply the website colors and type families
in frontend code. Use spacious layouts and short labels; do not copy the site's
navigation, marketing sections, or claims into an analysis. Preserve meaningful
legends, units, uncertainty, and sourcing when reducing copy.
