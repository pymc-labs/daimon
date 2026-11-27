"""Keep the hand-written docs pages readable by people.

The site grew by every change appending a paragraph to whichever page it
touched, until pages ran to thousands of lines. These limits make that fail
in CI instead: a page over budget gets cut or split, a long paragraph gets
broken up. Generated pages are exempt; their prose lives in the code.
stdlib-only; CI-shaped (default path: docs/).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

GENERATED = {"configuration.md", "mcp-tools.md"}
PAGE_WORDS = 2000
PAGE_WORDS_OVERRIDE = {"README.md": 500, "quickstart.md": 800, "features.md": 2727}
PARAGRAPH_WORDS = 100
SENTENCE_WORDS = 40
MAX_HEADING_LEVEL = 3

# "is new" is a time word at the end of a clause or before "in", "since"; "the value is new
# and not empty" is an adjective.
_TIME_WORD_RE = re.compile(
    r"\bnow\b|\bno longer\b|\bnewly\b|\bnew (?:in|since|as of|from)\b"
    r"|\b(?:is|are|was|were) new(?=\s*(?:[.,;:!?]|$)|\s+(?:in|since|as of|from)\b)",
    re.IGNORECASE,
)
_LITERAL_RE = re.compile(r"`[^`]*`|\*\*[^*]*\*\*|“[^”]*”|\"[^\"]*\"")  # code, labels, quotes
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z`\[(*])")


def _words(text: str) -> int:
    return len(_LINK_RE.sub(r"\1", text).split())


def _blocks(lines: list[str]) -> list[tuple[int, str]]:
    """Prose blocks (paragraphs, list items, quotes) as (first line, text)."""
    blocks: list[tuple[int, str]] = []
    buf: list[str] = []
    start, fenced = 0, False

    def flush() -> None:
        if buf:
            blocks.append((start, " ".join(buf)))
            buf.clear()

    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            flush()
            fenced = not fenced
            continue
        if fenced or not line or line.startswith(("|", "#", "<")):
            flush()
            continue
        if _LIST_RE.match(raw) or not buf:
            flush()
            start = number
        buf.append(line.lstrip(">").strip())
    flush()
    return blocks


def check(path: Path) -> list[str]:
    lines = path.read_text().splitlines()
    if lines[:1] == ["---"]:  # front matter: blank it, so line numbers stay true
        if "---" not in lines[1:]:
            return [f"{path}:1: front matter: opened with --- and never closed"]
        end = lines.index("---", 1)
        lines[: end + 1] = [""] * (end + 1)
    findings: list[str] = []
    blocks = _blocks(lines)
    budget = PAGE_WORDS_OVERRIDE.get(path.name, PAGE_WORDS)
    total = sum(_words(text) for _, text in blocks)
    if total > budget:
        findings.append(f"{path}:1: page: {total} words of prose, budget {budget}; cut or split")
    for number, text in blocks:
        if found := _TIME_WORD_RE.search(_LITERAL_RE.sub("", text)):
            findings.append(
                f"{path}:{number}: time word: {found.group(0)!r}; describe current behaviour"
            )
        if (n := _words(text)) > PARAGRAPH_WORDS:
            findings.append(f"{path}:{number}: paragraph: {n} words, max {PARAGRAPH_WORDS}")
            continue
        for sentence in _SENTENCE_END_RE.split(text):
            if (n := _words(sentence)) > SENTENCE_WORDS:
                findings.append(f"{path}:{number}: sentence: {n} words, max {SENTENCE_WORDS}")
    fenced = False
    for number, line in enumerate(lines, 1):
        if line.startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and line.startswith("#" * (MAX_HEADING_LEVEL + 1) + " "):
            findings.append(f"{path}:{number}: heading: deeper than H{MAX_HEADING_LEVEL}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Check docs pages against their length budgets.")
    parser.add_argument("root", nargs="?", default="docs", type=Path)
    args = parser.parse_args()
    if not (args.root / "features.md").is_file():  # generated, never committed, still checked
        print("docs/features.md is missing: run scripts/generate_feature_reference.py first")
        return 1
    pages = sorted(p for p in args.root.glob("*.md") if p.name not in GENERATED)
    findings = [f for page in pages for f in check(page)]
    print("\n".join(findings) or f"docs: {len(pages)} pages within limits")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
