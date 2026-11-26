#!/usr/bin/env python3
"""Write docs/llms.txt, the site's index for language models, from the mkdocs nav.

    generate_llms_txt.py            write docs/llms.txt
    generate_llms_txt.py --full OUT write every nav page into OUT (llms-full.txt)

Stdlib only. Never committed: it describes the generated reference pages, so run
scripts/generate_*.py first (the docs workflow and the image build do). The
description of a page is the first sentence of its first paragraph outside
web-only markers.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OPEN, CLOSE = "<!-- knowledge: web -->", "<!-- /knowledge -->"
SECTION = re.compile(r"^  - ([^:]+):\s*$")
ITEM = re.compile(r"^\s+- ([^:]+): ((?!https?:)\S+\.md)\s*$")


def _site_description() -> str:
    text = (ROOT / "mkdocs.yml").read_text()
    match = re.search(r"^site_description:\s*(.+)$", text, re.M)
    if not match:
        raise SystemExit("mkdocs.yml has no site_description")
    return match.group(1).strip()


def _nav() -> list[tuple[str, list[tuple[str, str]]]]:
    """Sections of (title, file); top-level pages go under "Docs"."""
    sections: list[tuple[str, list[tuple[str, str]]]] = [("Docs", [])]
    in_nav = False
    for line in (ROOT / "mkdocs.yml").read_text().splitlines():
        if line.startswith("nav:"):
            in_nav = True
        elif in_nav and line and not line.startswith(" "):
            break
        elif in_nav:
            if match := SECTION.match(line):
                sections.append((match.group(1), []))
            elif match := ITEM.match(line):
                sections[-1 if line.startswith("      ") else 0][1].append(match.groups())
    return [(name, items) for name, items in sections if items]


def _body(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"docs/{path.name} is missing: run scripts/generate_*.py first")
    text = path.read_text()
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)


def _description(path: Path) -> str:
    text = re.sub(rf"{re.escape(OPEN)}.*?{re.escape(CLOSE)}", "", _body(path), flags=re.S)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block or block[0] in "#<>|-*!`0123456789":
            continue
        flat = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", " ".join(block.split()))
        return re.split(r"(?<=[.!?])\s", flat, maxsplit=1)[0]
    raise SystemExit(f"{path.name}: no paragraph to describe the page")


def render() -> str:
    lines = ["# Daimon", "", f"> {_site_description()}", ""]
    for name, items in _nav():
        lines += [f"## {name}", ""]
        lines += [f"- [{title}]({file}): {_description(DOCS / file)}" for title, file in items]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_full() -> str:
    pages = [_body(DOCS / file).strip() for _, items in _nav() for _, file in items]
    return "\n\n---\n\n".join(pages) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", metavar="OUT", help="write all nav pages to OUT")
    args = parser.parse_args(argv)
    if args.full:
        Path(args.full).write_text(render_full())
        return 0
    (DOCS / "llms.txt").write_text(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
