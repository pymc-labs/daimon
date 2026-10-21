"""Render with uv run --no-project --with typst==0.15.0 --with mistune==3.2.0
python tests/render-install-kit.py --style-dir /path/to/pymc-artifact-style.
Check without render dependencies: python3 tests/render-install-kit.py --check
(requires Poppler pdftotext and pdfinfo). Rendering embeds the supplied brand fonts.
"""

import argparse
import json
import re
import subprocess
import tempfile
import unicodedata
from importlib import import_module
from pathlib import Path

parser = argparse.ArgumentParser(
    description="Render the kit, or check PDF text with Poppler pdftotext."
)
parser.add_argument("--check", action="store_true")
parser.add_argument(
    "--style-dir", type=Path, help="pymc-artifact-style skill directory (render only)"
)
args = parser.parse_args()

root = Path(__file__).resolve().parent.parent
style = args.style_dir
source = root / "deploy/helm/daimon-selfhost/INSTALL-COKE.md"
if args.check:
    markdown = source.read_text()
    plain = re.sub(r"^```[^\n]*\n", "", markdown, flags=re.M)
    plain = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", plain)
    plain = re.sub(r"^#+ ", "", plain, flags=re.M).replace("**", "").replace("`", "")
    pdf = subprocess.check_output(
        ["pdftotext", "-raw", str(source.with_suffix(".pdf")), "-"], text=True
    )

    def normalize(text):
        return "".join(unicodedata.normalize("NFKC", text).split())

    if normalize(plain) != normalize(pdf):
        raise SystemExit(
            "PDF text differs from Markdown; regenerate with tests/render-install-kit.py"
        )
    info = subprocess.check_output(["pdfinfo", str(source.with_suffix(".pdf"))], text=True)
    if not re.search(r"^Pages:\s+1$", info, re.M) or not re.search(
        r"^Page size:.*\(A4\)", info, re.M
    ):
        raise SystemExit("PDF must be one A4 page")
    print("PASS: PDF text matches Markdown; one A4 page")
    raise SystemExit(0)
if style is None:
    parser.error("--style-dir is required to render; install typst==0.15.0 and mistune==3.2.0")
mistune = import_module("mistune")
typst = import_module("typst")

base = "https://github.com/pymc-labs/daimon/blob/azure-selfhost/deploy/helm/daimon-selfhost/"


def quote(s):
    return json.dumps(s, ensure_ascii=False)


def content(tokens):
    out = []
    for token in tokens:
        kind = token["type"]
        children = content(token.get("children", []))
        if kind == "text":
            out.append("#text(" + quote(token["raw"]) + ")")
        elif kind in {"softbreak", "linebreak"}:
            out.append(" ")
        elif kind == "strong":
            out.append("#strong[" + children + "]")
        elif kind == "codespan":
            out.append("#raw(" + quote(token["raw"]) + ")")
        elif kind == "link":
            url = token["attrs"]["url"]
            if not url.startswith("https:"):
                url = base + url
            out.append("#link(" + quote(url) + ")[" + children + "]")
        elif kind == "heading":
            out.append('#text(size: 17pt, weight: "bold")[' + children + "]\n#v(2pt)\n")
        elif kind == "paragraph":
            out.append(children + "\n\n")
        elif kind == "block_code":
            out.append("#raw(" + quote(token["raw"].rstrip("\n")) + ", block: true)\n\n")
        elif kind != "blank_line":
            raise ValueError(kind)
    return "".join(out)


# The multipage report class adds a cover. This one-page operator sheet uses
# its bundled fonts, official logo and website palette with compact geometry.
setup = """#set document(title: "Daimon on Azure — Coke IT install kit", author: "PyMC Labs")
#set page(paper: "a4", margin: (x: 14mm, y: 12mm))
#set text(font: "Inter", size: 10pt, fill: rgb("#0C1F40"), hyphenate: false)
#set par(leading: 0.5em, spacing: 0.8em)
#show strong: set text(weight: "semibold")
#show link: underline.with(stroke: 0.3pt + rgb("#9FAAE2"))
#show raw.where(block: false): set text(font: "JetBrains Mono", size: 8pt)
#show raw.where(block: true): it => block(width: 100%, fill: rgb("#F7F7F7"), inset: 6pt, above: 4pt, below: 4pt)[
  #set text(font: "JetBrains Mono", size: 8.6pt)
  #set par(leading: 0.3em)
  #it
]
"""
setup += (
    "#image("
    + quote(str(style / "assets/pymc-labs-logo-dark.png"))
    + ', width: 24mm)\n#v(4pt)\n#line(length: 100%, stroke: 1pt + rgb("#B4E7DD"))\n#v(5pt)\n'
)
ast = mistune.create_markdown(renderer="ast")(source.read_text())
with tempfile.TemporaryDirectory() as scratch:
    target = Path(scratch) / "kit.typ"
    target.write_text(setup + content(ast))
    typst.compile(
        str(target),
        output=str(source.with_suffix(".pdf")),
        root="/",
        font_paths=[str(style / "fonts")],
    )
