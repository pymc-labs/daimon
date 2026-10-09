@app.cell(hide_code=True)
def _():
    # PyMC Labs house style for marimo — generated cell, do not edit.
    # Source of truth: pymc-artifact-style skill (notebook/style_cell.py).
    import base64
    import io
    import json
    import tempfile
    import zlib
    from pathlib import Path

    import marimo as mo
    import matplotlib as mpl
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    from matplotlib import font_manager as fm
    from matplotlib.colors import LinearSegmentedColormap

    ASSETS = json.loads(r'''__ASSETS_JSON__''')

    # ---- palette: the exact pymc-labs.com hexes ------------------------------
    C = dict(
        navy="#0C1F40", navy_deep="#07142A", aqua="#B4E7DD", periwinkle="#9FAAE2",
        peach="#F6AE72", violet="#C8B4E7", soft="#F7F7F7",
        teal="#0C9E82", indigo="#5462C4", orange="#C4720A",   # readable text accents
    )
    # fixed series order: strong first, pale last. Never reorder by rank.
    CYCLE = [C["navy"], C["teal"], C["peach"], C["indigo"], C["periwinkle"],
             C["orange"], C["violet"], C["aqua"]]
    SEQ = LinearSegmentedColormap.from_list("pymc_seq", ["#FFFFFF", C["aqua"], C["teal"], C["navy"]])
    DIV = LinearSegmentedColormap.from_list("pymc_div", [C["indigo"], "#FFFFFF", C["orange"]])

    # ---- fonts: embedded (Inter 400/500/600, JetBrains Mono 400) -------------
    _fonts = {k: zlib.decompress(base64.b64decode(ASSETS[k]))
              for k in ("inter_400", "inter_500", "inter_600", "mono_400")}
    _dir = Path(tempfile.mkdtemp(prefix="pymc_fonts_"))
    for _k, _v in _fonts.items():
        (_dir / f"{_k}.ttf").write_bytes(_v)
        fm.fontManager.addfont(str(_dir / f"{_k}.ttf"))

    def _face(family, weight, key):
        b64 = base64.b64encode(_fonts[key]).decode()
        return (f"@font-face{{font-family:'{family}';font-weight:{weight};font-style:normal;"
                f"font-display:swap;src:url(data:font/ttf;base64,{b64}) format('truetype');}}")

    FONT_CSS = (_face("Inter", 400, "inter_400") + _face("Inter", 500, "inter_500")
                + _face("Inter", 600, "inter_600") + _face("JetBrains Mono", 400, "mono_400"))

    mpl.rcParams.update({
        "font.family": "sans-serif", "font.sans-serif": ["Inter", "DejaVu Sans"],
        "font.size": 9.5, "axes.prop_cycle": mpl.cycler(color=CYCLE),
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
    LOGO = "data:image/png;base64," + ASSETS["logo_dark_png"]
    CSS = FONT_CSS + """
    :root{--pm-navy:#0C1F40;--pm-aqua:#B4E7DD;--pm-peri:#9FAAE2;--pm-peach:#F6AE72;
      --pm-soft:#F7F7F7;--pm-teal:#0C9E82;--pm-indigo:#5462C4;--pm-orange:#C4720A;
      --pm-line:#0c1f4014;color-scheme:light;
      /* marimo's own theme variables */
      --heading-font:'Inter',system-ui,sans-serif;--text-font:'Inter',system-ui,sans-serif;
      --monospace-font:'JetBrains Mono',ui-monospace,monospace;
      --primary:#0C9E82;--color-blue-500:#0C9E82;--color-blue-600:#0C9E82;--blue-9:#0C9E82;
      --color-slate-200:#0c1f4018;}
    html,body,#root{background:#fff!important;}
    body,.markdown,.prose,marimo-ui-element,label,button,input,select,table{
      font-family:'Inter',system-ui,-apple-system,sans-serif;color:var(--pm-navy);}
    body{line-height:1.55;font-weight:400;-webkit-font-smoothing:antialiased;}
    .markdown h1,.prose h1{font-weight:600;letter-spacing:-.02em;line-height:1.1;color:var(--pm-navy);}
    .markdown h2,.prose h2{font-weight:500;letter-spacing:-.015em;color:var(--pm-navy);
      margin-top:2.4rem;padding-top:1.1rem;border-top:1px solid var(--pm-line);}
    .markdown h3,.prose h3{font-weight:500;letter-spacing:-.01em;color:var(--pm-navy);}
    .markdown a,.prose a{color:var(--pm-indigo);text-decoration:none;border-bottom:1px solid #5462c455;}
    .markdown a:hover,.prose a:hover{color:#3a4fc4;}
    .markdown code,.prose code{background:var(--pm-soft);color:var(--pm-navy);padding:.1em .35em;
      border-radius:4px;font-size:.88em;font-family:'JetBrains Mono',ui-monospace,monospace;}
    .markdown pre,.prose pre{background:var(--pm-soft)!important;color:var(--pm-navy)!important;
      border:1px solid var(--pm-line);border-radius:8px;}
    .markdown blockquote,.prose blockquote{border-left:3px solid var(--pm-aqua);background:transparent;
      color:var(--pm-navy);padding-left:1rem;font-style:normal;}
    .pm-head{display:flex;flex-direction:column;gap:.9rem;padding:1.2rem 0 1.6rem;
      border-bottom:3px solid var(--pm-aqua);margin-bottom:1.6rem;}
    .pm-head img{height:34px;width:auto;align-self:flex-start;}
    .pm-head h1{margin:0;font-size:2.35rem;font-weight:600;letter-spacing:-.02em;line-height:1.05;}
    .pm-head h1 em{font-family:Georgia,serif;font-style:italic;font-weight:500;}
    .pm-head p{margin:0;font-size:1.05rem;color:#0c1f40cc;max-width:42rem;}
    .pm-meta{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;}
    .pm-badge{display:inline-block;font-size:.72rem;font-weight:500;letter-spacing:.02em;
      padding:.18rem .6rem;border-radius:999px;}
    .pm-badge.teal{color:var(--pm-teal);background:#b4e7dd59;}
    .pm-badge.orange{color:var(--pm-orange);background:#f6ae7259;}
    .pm-badge.indigo{color:var(--pm-indigo);background:#9faae240;}
    .pm-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.8rem;margin:.4rem 0 1rem;}
    .pm-kpi{border:1px solid var(--pm-line);border-top:3px solid var(--pm-aqua);border-radius:10px;
      padding:.8rem 1rem;background:#fff;}
    .pm-kpi b{display:block;font-size:1.6rem;font-weight:600;letter-spacing:-.02em;line-height:1.15;}
    .pm-kpi span{font-size:.78rem;color:#0c1f40b3;}
    .pm-callout{border-left:3px solid var(--pm-aqua);background:#b4e7dd2e;border-radius:0 8px 8px 0;
      padding:.75rem 1rem;margin:.6rem 0;font-size:.95rem;}
    .pm-callout.warn{border-left-color:var(--pm-peach);background:#f6ae7226;}
    .pm-callout p{margin:.2rem 0;}
    .pm-callout strong:first-child{color:var(--pm-teal);}
    .pm-callout.warn strong:first-child{color:var(--pm-orange);}
    .pm-fig{margin:.6rem 0 1.2rem;}
    .pm-fig img{width:100%;height:auto;display:block;}
    .pm-fig figcaption{font-size:.8rem;color:#0c1f40b3;margin-top:.35rem;max-width:44rem;}
    table.pm-table{display:table!important;border-collapse:collapse;width:auto!important;max-width:100%;font-size:.88rem;margin:.6rem 0;
      border-top:2px solid var(--pm-navy);border-bottom:2px solid var(--pm-navy);}
    table.pm-table th{font-weight:600;text-align:right;padding:.45rem .7rem;
      border:none;background:transparent;}
    table.pm-table thead tr:last-child th{border-bottom:1px solid var(--pm-navy);}
    table.pm-table tbody th{font-weight:500;}
    table.pm-table td{text-align:right;padding:.38rem .7rem;border:none;font-variant-numeric:tabular-nums;}
    table.pm-table th:first-child,table.pm-table td:first-child{text-align:left;}
    table.pm-table tbody tr:nth-child(even){background:var(--pm-soft);}
    input[type=range],input[type=checkbox],input[type=radio]{accent-color:var(--pm-teal);}
    """

    def badge(text, color="teal"):
        return f'<span class="pm-badge {color}">{text}</span>'

    def header(title, subtitle="", badges=(), accent=""):
        """Page header: logo, title, subtitle, badges. `accent` = one word of the
        title set in the serif accent (never body text). badges=[(text, 'teal'|'orange'|'indigo')]"""
        t = title.replace(accent, f"<em>{accent}</em>", 1) if accent else title
        bs = "".join(badge(b, c) for b, c in badges)
        return mo.Html(
            f"<style>{CSS}</style><div class='pm-head'><img src='{LOGO}' alt='PyMC Labs'/>"
            f"<h1>{t}</h1>" + (f"<p>{subtitle}</p>" if subtitle else "")
            + (f"<div class='pm-meta'>{bs}</div>" if bs else "") + "</div>"
        )

    def kpis(items):
        """items = [(value, label), ...] -> key-number cards."""
        cards = "".join(f"<div class='pm-kpi'><b>{v}</b><span>{l}</span></div>" for v, l in items)
        return mo.Html(f"<div class='pm-kpis'>{cards}</div>")

    def callout(md_text, kind="info"):
        """Callout. Start with **Label.** — it is coloured. kind: 'info' (aqua/teal) | 'warn' (peach/dark-orange)."""
        cls = "pm-callout warn" if kind == "warn" else "pm-callout"
        return mo.Html(f"<div class='{cls}'>{mo.md(md_text).text}</div>")

    def figure(fig, caption="", dpi=200):
        """Matplotlib figure -> inline PNG (fonts baked in) with a caption; closes the figure."""
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
        plt.close(fig)
        b64 = base64.b64encode(buf.getvalue()).decode()
        cap = f"<figcaption>{caption}</figcaption>" if caption else ""
        return mo.Html(f"<figure class='pm-fig'><img src='data:image/png;base64,{b64}'/>{cap}</figure>")

    def table(df, fmt="{:,.2f}", index=True):
        """DataFrame -> rules-only table (heavy top/bottom, light header rule, no verticals)."""
        return mo.Html(df.to_html(classes="pm-table", border=0, index=index,
                                  float_format=lambda v: fmt.format(v)))

    return (C, CYCLE, DIV, SEQ, badge, callout, figure, header, kpis, mo, np, pd, plt, table)
