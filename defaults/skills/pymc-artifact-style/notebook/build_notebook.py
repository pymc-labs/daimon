#!/usr/bin/env python3
"""Assemble a PyMC-styled marimo notebook.

    python build_notebook.py content.py -o nb.py [--title "Tab title"] [--width medium]

`content.py` holds ONLY your content cells (``@app.cell`` functions). They may use
the names the style cell exports: mo, np, pd, plt, C, CYCLE, SEQ, DIV, header(),
kpis(), callout(), figure(), table(), badge(). The build wraps them with the
marimo preamble, the generated style cell (fonts + logo embedded), and the footer.
"""
import argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent

ap = argparse.ArgumentParser()
ap.add_argument("content")
ap.add_argument("-o", "--out", required=True)
ap.add_argument("--title", default="PyMC Labs")
ap.add_argument("--width", default="medium", choices=["compact", "medium", "full"])
a = ap.parse_args()

assets = (HERE / "assets.json").read_text()
style = (HERE / "style_cell.py").read_text().replace("__ASSETS_JSON__", assets)
content = Path(a.content).read_text().strip("\n")
src = (
    "import marimo\n\n"
    f'app = marimo.App(width="{a.width}", app_title={a.title!r})\n\n\n'
    + style.rstrip("\n") + "\n\n\n" + content + "\n\n\n"
    'if __name__ == "__main__":\n    app.run()\n'
)
Path(a.out).write_text(src)
print(f"wrote {a.out} ({len(src)/1024:.0f} KiB)")
