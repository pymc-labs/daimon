#!/usr/bin/env python3
"""Assemble a PyMC-styled single-file dashboard from the frozen template.

Usage: build_dashboard.py content.html -o dashboard.html
                          [--title T] [--subtitle S] [--footer F] [--scripts scripts.js]

content.html = the <section class="tab" id="..." data-title="..."> blocks only.
scripts.js   = the Plotly calls (wrap in <script>…</script> or pass raw JS).
The template carries the frozen style block, the wordmark header (inlined here
from the bundled logo), tab wiring, and PYMC_LAYOUT/PYMC_CONFIG Plotly defaults.

Guard: any hex literal in the content that is not a palette value fails the
build (non-negotiable 2) — use var(--token), the C.* JS constants, or rgba().
"""
import argparse
import base64
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PALETTE = {"0C1F40", "07142A", "071530", "B4E7DD", "9FAAE2", "F6AE72", "C8B4E7",
           "F7F7F7", "FFFFFF", "0C9E82", "5462C4", "C4720A"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("content")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--title", default="MMM Dashboard")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--footer", default="PyMC Labs")
    ap.add_argument("--scripts", help="JS file with the Plotly calls")
    a = ap.parse_args()

    content = open(a.content, encoding="utf-8").read()
    scripts = open(a.scripts, encoding="utf-8").read() if a.scripts else ""
    if scripts and "<script" not in scripts:
        scripts = "<script>\n%s\n</script>" % scripts

    bad = {h.upper() for h in re.findall(r"#([0-9a-fA-F]{6})\b", content + scripts)} - PALETTE
    if bad:
        sys.exit("non-palette hexes in content: %s — use var(--token)/C.*/rgba()" % sorted(bad))

    logo = base64.b64encode(
        open(os.path.join(HERE, "..", "assets", "pymc-labs-logo-light.png"), "rb").read()
    ).decode()
    html = open(os.path.join(HERE, "dashboard_template.html"), encoding="utf-8").read()
    for k, v in (("{{TITLE}}", a.title), ("{{SUBTITLE}}", a.subtitle), ("{{FOOTER}}", a.footer),
                 ("{{LOGO_B64}}", logo), ("{{CONTENT}}", content), ("{{SCRIPTS}}", scripts)):
        html = html.replace(k, v)
    open(a.out, "w", encoding="utf-8").write(html)
    print("wrote %s (%.0f KB)" % (a.out, len(html) / 1024))


if __name__ == "__main__":
    main()
