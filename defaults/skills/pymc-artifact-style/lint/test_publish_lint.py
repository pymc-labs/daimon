#!/usr/bin/env python3
"""Self-test for publish_lint.py: the bundled page examples pass, the clutter fixture fails on every check."""
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import publish_lint  # noqa: E402

EXPECT = {"accordion", "expand-collapse", "sticky", "sidebar", "tabs", "gradient", "shadow", "dark-band", "hero",
          "decorative-svg", "serif-accent", "emoji", "pills", "cards", "icon-cards", "callout", "copy-non-prompt",
          "copy-multiple", "buttons", "small-text", "boilerplate-labels", "heading-flourish", "phrases", "bold",
          "generic-headings", "heading-links", "rounded", "eyebrow"}

fail = 0
got = {r["check"] for r in publish_lint.lint((HERE / "fixtures" / "clutter.html").read_text(), "page", HERE / "fixtures")}
missing = EXPECT - got
if missing:
    print("FAIL clutter fixture: checks that did not fire:", sorted(missing)); fail = 1
else:
    print(f"ok   clutter fixture: all {len(EXPECT)} checks fire")

with tempfile.TemporaryDirectory() as d:
    for md in sorted((SKILL / "page" / "examples").glob("*.md")):
        out = Path(d) / (md.stem + ".html")
        subprocess.run([sys.executable, str(SKILL / "page" / "build_page.py"), str(md), "-o", str(out)],
                       check=True, capture_output=True)
        res = publish_lint.lint(out.read_text(), "page", out.parent)
        blocks = [r["check"] for r in res if r["level"] == "BLOCK"]
        warns = [r["check"] for r in res if r["level"] == "WARN"]
        if blocks or warns:
            print(f"FAIL {md.name}: built page has findings {blocks + warns}"); fail = 1
        else:
            print(f"ok   {md.name}: built page is lint-clean")
sys.exit(fail)
