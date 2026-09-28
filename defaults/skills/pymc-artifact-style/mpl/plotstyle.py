"""PyMC Labs matplotlib style helpers.

The global look (website palette, Inter font, thin white marker edges,
text-column-width default figure) lives in ``pymclabsreport/matplotlibrc`` (and the
selectable ``pymclabs`` style). One thing matplotlibrc cannot express is
``fill_between``'s default edge: passing ``color=`` makes the filled band's edge
the same color as its face, leaving a faint hairline. Importing this module
patches ``Axes.fill_between`` / ``fill_betweenx`` to default ``edgecolor="none"``
(explicit ``edgecolor`` still wins), so filled bands are clean by default.

    import pymclabsreport.plotstyle  # noqa: F401  — applies the patch on import

(``import pymclabsreport`` does this for you and also activates the rc.)
It also exposes the palette as Python dicts for convenient reference.
"""

import functools

import matplotlib.axes as _maxes

# Website palette (hex), mirroring the matplotlibrc header. Source:
# pymc-labs.com MarketingLayout.BTaXrrww.css + inline styles, fetched 2026-09-28.
PALETTE = {
    "navy": "#0C1F40",
    "navy_deep": "#07142A",
    "periwinkle": "#9FAAE2",
    "aqua": "#B4E7DD",
    "peach": "#F6AE72",
    "violet": "#C8B4E7",
    "soft_white": "#F7F7F7",
}
# Text accents: the site's readable-on-white colors for text, labels and
# annotations. The pale fills above are for areas and bands, not small text.
PALETTE_TEXT = {
    "teal": "#0C9E82",
    "indigo": "#5462C4",
    "dark_orange": "#C4720A",
}
# Series order of the matplotlibrc color cycle: strong colors first, pale last.
CYCLE = ["#0C1F40", "#0C9E82", "#F6AE72", "#5462C4",
         "#9FAAE2", "#C4720A", "#C8B4E7", "#B4E7DD"]


def _patch_fill_between():
    for name in ("fill_between", "fill_betweenx"):
        orig = getattr(_maxes.Axes, name)
        if getattr(orig, "_pymclabs_patched", False):
            continue

        def _wrap(orig):
            @functools.wraps(orig)
            def wrapper(self, *args, **kwargs):
                kwargs.setdefault("edgecolor", "none")
                return orig(self, *args, **kwargs)

            wrapper._pymclabs_patched = True
            return wrapper

        setattr(_maxes.Axes, name, _wrap(orig))


_patch_fill_between()


def demo(path="pymclabs_style_demo.png"):
    """Render a swatch + sample plot using the global pymclabs style."""
    import matplotlib.pyplot as plt
    import numpy as np

    rows = [("fills", PALETTE), ("text", PALETTE_TEXT)]
    fig, (a0, a1) = plt.subplots(1, 2, figsize=(9.6, 3.0))
    for r, (lab, pal) in enumerate(rows):
        for c, (k, v) in enumerate(pal.items()):
            a0.add_patch(plt.Rectangle((c, -1.5 * r), 0.92, 0.92, color=v))
            a0.text(c + 0.46, -1.5 * r - 0.08, k, ha="center", va="top", fontsize=6)
        a0.text(-0.15, -1.5 * r + 0.46, lab, ha="right", va="center", fontsize=8)
    a0.set_xlim(-1.3, 7.2)
    a0.set_ylim(-2.0, 1.2)
    a0.axis("off")
    a0.set_title("palette  (fills / text accents)")

    x = np.linspace(0, 10, 60)
    for i in range(6):
        a1.plot(x, np.sin(x + i * 0.5) + i * 0.4, marker="o", ms=4, markevery=7)
    a1.fill_between(x, -1.4, -0.9 + 0.25 * np.sin(x), alpha=0.55, color=PALETTE["aqua"])
    a1.set_title("cycle + markers (white edge) + clean fill_between")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"Saved {path}")


if __name__ == "__main__":
    demo()
