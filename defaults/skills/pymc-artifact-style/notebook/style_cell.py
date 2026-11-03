@app.cell(hide_code=True)
def _():
    # PyMC Labs house style for marimo — generated cell, do not edit.
    # Source of truth: pymc-artifact-style skill (notebook/style_cell.py).
    import base64 as _base64
    import io as _io
    import json as _json
    import tempfile as _tempfile
    import zlib as _zlib
    from pathlib import Path as _Path

    import marimo as mo
    import matplotlib as _mpl
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    from matplotlib import font_manager as _fm
    from matplotlib.colors import LinearSegmentedColormap as _LSC

    _ASSETS = _json.loads(r'''__ASSETS_JSON__''')

    # ---- palette: the exact pymc-labs.com hexes ------------------------------
    C = dict(
        navy="#0C1F40", navy_deep="#07142A", aqua="#B4E7DD", periwinkle="#9FAAE2",
        peach="#F6AE72", violet="#C8B4E7", soft="#F7F7F7",
        teal="#0C9E82", indigo="#5462C4", orange="#C4720A",   # readable text accents
    )
    # fixed series order: strong first, pale last. Never reorder by rank.
    CYCLE = [C["navy"], C["teal"], C["peach"], C["indigo"], C["periwinkle"],
             C["orange"], C["violet"], C["aqua"]]
    SEQ = _LSC.from_list("pymc_seq", ["#FFFFFF", C["aqua"], C["teal"], C["navy"]])
    DIV = _LSC.from_list("pymc_div", [C["indigo"], "#FFFFFF", C["orange"]])

    # ---- fonts: embedded (Inter 400/500/600, JetBrains Mono 400) -------------
    _fonts = {k: _zlib.decompress(_base64.b64decode(_ASSETS[k]))
              for k in ("inter_400", "inter_500", "inter_600", "mono_400")}
    _dir = _Path(_tempfile.mkdtemp(prefix="pymc_fonts_"))
    for _k, _v in _fonts.items():
        (_dir / f"{_k}.ttf").write_bytes(_v)
        _fm.fontManager.addfont(str(_dir / f"{_k}.ttf"))

    def _face(family, weight, key):
        b64 = _base64.b64encode(_fonts[key]).decode()
        return (f"@font-face{{font-family:'{family}';font-weight:{weight};font-style:normal;"
                f"font-display:swap;src:url(data:font/ttf;base64,{b64}) format('truetype');}}")

    _FONT_CSS = (_face("Inter", 400, "inter_400") + _face("Inter", 500, "inter_500")
                + _face("Inter", 600, "inter_600") + _face("JetBrains Mono", 400, "mono_400"))

    _mpl.rcParams.update({
        "font.family": "sans-serif", "font.sans-serif": ["Inter", "DejaVu Sans"],
        "font.size": 9.5, "axes.prop_cycle": _mpl.cycler(color=CYCLE),
        "text.color": C["navy"], "axes.edgecolor": C["navy"], "axes.labelcolor": C["navy"],
        "xtick.color": C["navy"], "ytick.color": C["navy"], "axes.linewidth": 0.8,
        "axes.titlecolor": C["navy"], "axes.titleweight": "medium",
        "axes.titlesize": "medium", "axes.titlelocation": "left",
        "axes.spines.top": False, "axes.spines.right": False,
        "lines.linewidth": 1.8, "lines.markeredgecolor": "white",
        "lines.markeredgewidth": 0.6, "scatter.edgecolors": "white",
        "legend.frameon": False, "legend.fontsize": 8.5,
        "grid.color": C["navy"], "grid.alpha": 0.12, "grid.linewidth": 0.6,
        "figure.figsize": (7.2, 3.6), "figure.dpi": 100,
        "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
    })

    # ---- page CSS ------------------------------------------------------------
    _LOGO = "data:image/png;base64," + _ASSETS["logo_dark_png"]
    _CSS = _FONT_CSS + """
    :root{--pm-navy:#0C1F40;--pm-aqua:#B4E7DD;--pm-peri:#9FAAE2;--pm-peach:#F6AE72;
      --pm-soft:#F7F7F7;--pm-teal:#0C9E82;--pm-indigo:#5462C4;--pm-orange:#C4720A;
      --pm-line:#0c1f4014;color-scheme:light;
      /* marimo's own theme variables */
      --heading-font:'Inter',system-ui,sans-serif;--text-font:'Inter',system-ui,sans-serif;
      --monospace-font:'JetBrains Mono',ui-monospace,monospace;
      --primary:#5462C4;--color-blue-500:#5462C4;--color-blue-600:#5462C4;--blue-9:#5462C4;
      --color-slate-200:#0c1f4018;}
    html,body,#root{background:#fff!important;}
    body,.markdown,.prose,marimo-ui-element,label,button,input,select,table{
      font-family:'Inter',system-ui,-apple-system,sans-serif;color:var(--pm-navy);}
    body{line-height:1.55;font-weight:400;-webkit-font-smoothing:antialiased;}
    body{font-size:18px;}
    .markdown,.prose{font-size:18px;line-height:1.6;}
    .markdown h1,.prose h1{font-weight:600;letter-spacing:-.02em;line-height:1.15;color:var(--pm-navy);}
    .markdown h2,.prose h2{font-weight:600;letter-spacing:-.015em;color:var(--pm-navy);margin-top:2.6rem;}
    .markdown h3,.prose h3{font-weight:600;color:var(--pm-navy);}
    .markdown a,.prose a{color:var(--pm-indigo);}
    .markdown code,.prose code{background:var(--pm-soft);color:var(--pm-navy);padding:.1em .3em;
      font-size:.85em;font-family:'JetBrains Mono',ui-monospace,monospace;}
    .markdown pre,.prose pre{background:var(--pm-soft)!important;color:var(--pm-navy)!important;
      border:1px solid var(--pm-line);}
    .pm-head{padding:.4rem 0 1.2rem;margin-bottom:1rem;}
    .pm-head img{height:20px;width:auto;display:block;margin-bottom:2rem;}
    .pm-head h1{margin:0 0 .6rem;font-size:2.4rem;font-weight:600;letter-spacing:-.02em;line-height:1.15;}
    .pm-head p{margin:0 0 .4rem;font-size:1.25rem;line-height:1.5;max-width:44rem;}
    .pm-meta{font-size:1rem;color:#0c1f40b8;}
    .pm-keytable{margin:.4rem 0 1.2rem;}
    .pm-note{margin:.6rem 0;font-size:1.05rem;}
    .pm-fig{margin:.6rem 0 1.2rem;}
    .pm-fig img{width:100%;height:auto;display:block;}
    .pm-fig figcaption{font-size:.95rem;color:#0c1f40b8;margin-top:.35rem;max-width:44rem;}
    table.pm-table{display:table!important;border-collapse:collapse;width:auto!important;max-width:100%;font-size:1rem;margin:.6rem 0;
      border-top:2px solid var(--pm-navy);border-bottom:2px solid var(--pm-navy);}
    table.pm-table th{font-weight:600;text-align:right;padding:.45rem .7rem;
      border:none;background:transparent;}
    table.pm-table thead tr:last-child th{border-bottom:1px solid var(--pm-navy);}
    table.pm-table tbody th{font-weight:500;}
    table.pm-table td{text-align:right;padding:.38rem .7rem;border:none;font-variant-numeric:tabular-nums;}
    table.pm-table th:first-child,table.pm-table td:first-child{text-align:left;}
    input[type=range],input[type=checkbox],input[type=radio]{accent-color:var(--pm-indigo);}
    """

    # The helpers keep their names so existing notebooks still run, but the
    # output is plain: no pills, no cards, no coloured callout boxes.
    def badge(text, color="teal"):
        """Plain status text (no pill). `color` is accepted and ignored."""
        return f'<span class="pm-status">{text}</span>'
    def header(title, subtitle="", badges=(), accent=""):
        """Page header: small wordmark, a title that states the point, one lede line.
        badges=[(text, _)] become one plain line of status text ("Example data · Draft").
        `accent` is accepted for old notebooks and ignored: no styled words in titles."""
        meta = " · ".join(b for b, _c in badges)
        return mo.Html(
            f"<style>{_CSS}</style><div class='pm-head'><img src='{_LOGO}' alt='PyMC Labs'/>"
            f"<h1>{title}</h1>" + (f"<p>{subtitle}</p>" if subtitle else "")
            + (f"<div class='pm-meta'>{meta}</div>" if meta else "") + "</div>"
        )
    def kpis(items):
        """items = [(value, label), ...] -> a rules-only two-column table, not cards."""
        rows = "".join(f"<tr><td>{l}</td><td>{v}</td></tr>" for v, l in items)
        return mo.Html(f"<table class='pm-table pm-keytable'><tbody>{rows}</tbody></table>")
    def callout(md_text, kind="info"):
        """A plain paragraph where the caveat applies (no box). Start with **Label.** if needed."""
        return mo.Html(f"<div class='pm-note'>{mo.md(md_text).text}</div>")
    def figure(fig, caption="", dpi=200):
        """Matplotlib figure -> inline PNG (fonts baked in) with a caption; closes the figure."""
        buf = _io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
        plt.close(fig)
        b64 = _base64.b64encode(buf.getvalue()).decode()
        cap = f"<figcaption>{caption}</figcaption>" if caption else ""
        return mo.Html(f"<figure class='pm-fig'><img src='data:image/png;base64,{b64}'/>{cap}</figure>")

    def table(df, fmt="{:,.2f}", index=True):
        """DataFrame -> rules-only table (heavy top/bottom, light header rule, no verticals)."""
        return mo.Html(df.to_html(classes="pm-table", border=0, index=index,
                                  float_format=lambda v: fmt.format(v)))

    return (C, CYCLE, DIV, SEQ, badge, callout, figure, header, kpis, mo, np, pd, plt, table)
