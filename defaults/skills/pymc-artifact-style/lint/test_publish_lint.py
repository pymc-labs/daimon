#!/usr/bin/env python3
"""Self-test for publish_lint.py and page/build_page.py: the bundled page examples build and pass,
the clutter fixture fails on every check, and review regressions stay fixed."""
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

# Regressions from review: each (html, check, should_fire)
HEAD = "<style>@font-face{font-family:Inter;src:url(x)} body{font:400 19px/1.6 Inter}</style>"
CASES = [
    (HEAD + "<main><h1>T</h1>" + "".join(f"<figure><img src='{i}.png'><figcaption>c</figcaption></figure>" for i in range(3)) + "</main>", "icon-cards", False),
    (HEAD + "<ul>" + "<li>item" * 3000 + "</ul>", "accordion", False),
    (HEAD + "<h1>T</h1><p>The results show all channels positive.</p>", "expand-collapse", False),
    (HEAD + "<style>table tbody{font-size:14px}</style><h1>T</h1>", "small-text", False),
    (HEAD + "<h1>T</h1><div class='side-by-side hdi-band status warn tag-cloud'>x</div>", "sidebar", False),
    (HEAD + "<h1>T</h1><div class='side-by-side hdi-band status warn tag-cloud'>x</div>", "hero", False),
    (HEAD + "<h1>T</h1><div class='side-by-side hdi-band status warn tag-cloud'>x</div>", "callout", False),
    (HEAD + "<h1>T</h1><div class='side-by-side hdi-band status warn tag-cloud'>x</div>", "pills", False),
    (HEAD + "<h1>T</h1><svg viewBox='0 0 340 250' style='width:100%'><rect/></svg>", "decorative-svg", True),
    (HEAD + "<style>h1 span{font:italic 500 20px Georgia}</style><h1>T</h1>", "serif-accent", True),
    (HEAD + "<style>header{background:#111827}</style><h1>T</h1>", "dark-band", True),
    (HEAD + "<style>.x{background:var(--navy-10)} pre{background:#000}</style><h1>T</h1>", "dark-band", False),
    (HEAD + "<h1>T</h1><p>Passed ✓, failed ✗, rated ★.</p>", "emoji", False),
    (HEAD + "<h1>T</h1><span class='pm-status'>Example data</span><table class='pm-table pm-keytable'></table>", "pills", False),
    (HEAD + "<h1>T</h1><span class='pm-status'>Example data</span><table class='pm-table pm-keytable'></table>", "cards", False),
    (HEAD + "<h1>T</h1><p>Here is the thing: it works.</p>", "phrases", True),
]
for i, (html, check, fire) in enumerate(CASES):
    got_c = {r["check"] for r in publish_lint.lint(html)}
    if (check in got_c) != fire:
        print(f"FAIL case {i}: {check} {'did not fire' if fire else 'fired'}"); fail = 1
print(f"ok   {len(CASES)} regression cases run")

sys.path.insert(0, str(SKILL / "page"))
import build_page  # noqa: E402

EDGE = {
    "[a](https://x.com/?a=1&b=2)": ('href="https://x.com/?a=1&amp;b=2"', "&amp;amp;"),
    "[w](https://en.wikipedia.org/wiki/Mean_(statistics))": ('Mean_(statistics)"', None),
    "[bad](javascript:alert(1))": ('href="#"', "javascript"),
    "[s](https://x/a/*b*/c)": ('href="https://x/a/*b*/c"', "<em>"),
    '![x](https://e/x.png"onerror="alert`1`)': ("&quot;onerror", 'onerror="'),
    "| a | b |\n|---|---|\n| x \\| y | `p|q` |": ("<td>x | y</td><td><code>p|q</code></td>", None),
}
for md, (want, unwanted) in EDGE.items():
    out = build_page.render("# T\n\n" + md, SKILL)
    if want not in out or (unwanted and unwanted in out):
        print(f"FAIL builder edge case {md!r}: {out[-160:]!r}"); fail = 1
print(f"ok   {len(EDGE)} builder edge cases")

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
