#!/usr/bin/env python3
"""Pre-publish lint for PyMC Labs HTML artifacts. Run it before publishing any HTML.

    python3 publish_lint.py page.html [--kind page|dashboard] [--render] [--json]

Exit code 1 if any BLOCK finding: fix them and run again. WARN findings go in the
send-readiness note. Standard library only; --render adds computed-style checks
(body text size, phone overflow) through Playwright when it is installed.

--kind dashboard allows what an interactive dashboard needs (tab buttons, selects,
sliders); everything else is checked the same way.
"""
import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000026FF\U00002700-\U000027BF\U0001F1E6-\U0001F1FF⭐️]")
PHRASES = [
    r"\bnot just\b", r"\bnot only\b[^.]{0,80}\bbut\b", r"here'?s the thing", r"let'?s dive in", r"\bdive into\b",
    r"\bin today'?s\b", r"\bgame[- ]changer\b", r"\bseamless(ly)?\b", r"\bunlock(s|ing)?\b", r"\bi hope this helps\b",
    r"\bit'?s worth noting\b", r"\bdelve\b", r"\btapestry\b", r"\bat the end of the day\b", r"\bwithout further ado\b",
    r"\bin conclusion\b", r"\bnavigat(e|ing) the (complex|landscape)", r"\brobust\b", r"\bleverag(e|ing)\b",
]
LABELS = [r"good to know", r"key takeaways?", r"at a glance", r"prepared by\b", r"tl;?dr", r"pro tip", r"heads up"]
GENERIC_H = {"results", "overview", "summary", "method", "methods", "background", "next steps", "introduction",
             "conclusion", "details", "context", "findings", "notes", "about"}
DARK_BG = re.compile(r"background(-color)?\s*:[^;}\"]*(#0C1F40|#07142A|#071530|--navy-header|--navy-deep|--navy\b|rgb\(\s*[0-3]?\d\s*,\s*[0-4]?\d\s*,\s*[0-7]?\d\s*\))", re.I)
CLUTTER_CLASS = {
    "pills": re.compile(r"(^|[-_ ])(badge|pill|chip|tag|label-pill)s?($|[-_ ])", re.I),
    "cards": re.compile(r"(^|[-_ ])(card|tile|kpi)s?($|[-_ ])", re.I),
    "hero": re.compile(r"(^|[-_ ])(hero|band|banner|masthead|jumbotron)($|[-_ ])", re.I),
    "eyebrow": re.compile(r"(^|[-_ ])(eyebrow|kicker|overline)($|[-_ ])", re.I),
    "callout": re.compile(r"(^|[-_ ])(callout|admonition|alert|notice|tip|warn)($|[-_ ])", re.I),
}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children, self.text = tag, dict(attrs), parent, [], []

    def cls(self):
        return self.attrs.get("class") or ""

    def all_text(self):
        out = list(self.text)
        for c in self.children:
            out.append(c.all_text())
        return " ".join(t for t in out if t).strip()

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root
        self.css = []

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.cur)
        self.cur.children.append(n)
        if n.attrs.get("style"):
            self.css.append(n.attrs["style"])
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.cur = self.cur.parent

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        if self.cur.tag == "style":
            self.css.append(data)
        elif self.cur.tag != "script" and data.strip():
            self.cur.text.append(data.strip())


def visible_text_nodes(root):
    for n in root.walk():
        if n.tag in ("script", "style", "head", "title") or any(a.tag in ("script", "style", "head") for a in n.ancestors()):
            continue
        for t in n.text:
            yield n, t


def lint(html, kind="page", base=None):
    t = Tree()
    t.feed(html)
    if base is not None:  # local stylesheets count as the page's CSS
        for n in t.root.walk():
            href = n.attrs.get("href") or ""
            if n.tag == "link" and "stylesheet" in (n.attrs.get("rel") or "") and not re.match(r"^(https?:)?//", href):
                f = Path(base) / href
                if f.exists():
                    t.css.append(f.read_text(encoding="utf-8", errors="replace"))
    root = t.root
    css = "\n".join(t.css)
    css_nofonts = re.sub(r"@font-face\s*\{[^}]*\}", "", css)
    css_nofonts = re.sub(r"url\(data:[^)]*\)", "url()", css_nofonts)
    nodes = list(root.walk())
    find = []

    def add(level, cid, msg, n=1):
        if n:
            find.append({"level": level, "check": cid, "message": msg, "count": n})

    # --- hidden content --------------------------------------------------
    add("BLOCK", "accordion", "accordions hide content: show it, or cut it",
        sum(1 for n in nodes if n.tag in ("details", "summary")) + sum(1 for n in nodes if "aria-expanded" in n.attrs))
    txt_all = " ".join(t for _, t in visible_text_nodes(root))
    add("BLOCK", "expand-collapse", "expand / collapse controls",
        len(re.findall(r"\b(expand|collapse) all\b|\bshow (more|all)\b", txt_all, re.I)))

    # --- navigation ------------------------------------------------------
    add("BLOCK", "sticky", "sticky or fixed positioning (sticky nav, floating bars)",
        len(re.findall(r"position\s*:\s*(sticky|fixed)", css_nofonts, re.I)))
    add("BLOCK", "sidebar", "sidebar (<aside> or a nav beside the content)",
        sum(1 for n in nodes if n.tag == "aside" or re.search(r"(^|[-_ ])(side|sidebar|toc)($|[-_ ])", n.cls(), re.I)))
    if kind != "dashboard":
        add("BLOCK", "tabs", "tabs or tab chips on a document", sum(1 for n in nodes if n.tag == "nav" or n.attrs.get("role") == "tablist"))

    # --- decoration ------------------------------------------------------
    add("BLOCK", "gradient", "gradients",
        len(re.findall(r"(linear|radial|conic)-gradient\(", css_nofonts, re.I))
        + sum(1 for n in nodes if n.tag.lower() in ("lineargradient", "radialgradient")))
    add("BLOCK", "shadow", "shadows",
        len([m for m in re.findall(r"(box-shadow|text-shadow)\s*:\s*([^;}\"]*)", css_nofonts, re.I) if m[1].strip() != "none"])
        + len(re.findall(r"drop-shadow\(", css_nofonts, re.I)))
    add("BLOCK", "dark-band", "dark header band or dark section background", len(DARK_BG.findall(css_nofonts)))
    add("BLOCK", "hero", "hero / banner block", sum(1 for n in nodes if CLUTTER_CLASS["hero"].search(n.cls())))
    svgs = [n for n in nodes if n.tag == "svg" and not any(a.tag in ("button", "a") for a in n.ancestors())
            and not any(a.tag == "figure" for a in n.ancestors())]
    add("BLOCK", "decorative-svg", "inline SVG art outside a figure (hero art, mock UI, icons)",
        sum(1 for n in svgs if int(re.sub(r"\D", "", n.attrs.get("width", "0") or "0") or 0) > 40 or "hero" in n.cls() or "art" in n.cls()))
    add("BLOCK", "serif-accent", "serif accent font (one family only: Inter, plus JetBrains Mono for code)",
        len(re.findall(r"font-family\s*:[^;}]*\b(georgia|times|garamond|(?<!sans-)serif)\b", css_nofonts, re.I)))
    add("BLOCK", "emoji", "emoji", len(EMOJI.findall(txt_all)))

    # --- components ------------------------------------------------------
    pills = sum(1 for n in nodes if CLUTTER_CLASS["pills"].search(n.cls()))
    pills += len(re.findall(r"border-radius\s*:\s*(999|9999|100|50%|2em|1\.?\d*rem)", css_nofonts, re.I))
    add("BLOCK", "pills", "pills, badges or chips", pills)
    cards = [n for n in nodes if CLUTTER_CLASS["cards"].search(n.cls())]
    add("BLOCK", "cards", "cards or key-number tiles", len(cards))
    icon_cards = 0
    for n in nodes:
        kids = [c for c in n.children if c.tag not in ("script", "style")]
        if len(kids) >= 3 and sum(1 for c in kids if any(d.tag in ("svg", "i") or (d.tag == "img" and "mark" not in d.cls()) for d in c.walk() if d is not c)) >= 3:
            icon_cards += 1
    add("BLOCK", "icon-cards", "grid of items that each start with an icon", icon_cards)
    radius = [float(x) for x in re.findall(r"border-radius\s*:\s*(\d+(?:\.\d+)?)px", css_nofonts, re.I)]
    add("WARN", "rounded", "rounded corners over 4px (rounded-card look)", sum(1 for r in radius if r > 4))
    add("BLOCK", "callout", "callout / admonition boxes: say it in a sentence where it applies",
        sum(1 for n in nodes if CLUTTER_CLASS["callout"].search(n.cls())) + sum(1 for n in nodes if n.tag == "blockquote"))
    add("WARN", "eyebrow", "eyebrow / kicker label above a heading",
        sum(1 for n in nodes if CLUTTER_CLASS["eyebrow"].search(n.cls())) + len(re.findall(r"text-transform\s*:\s*uppercase", css_nofonts, re.I)))

    # --- buttons ---------------------------------------------------------
    buttons = [n for n in nodes if n.tag == "button" or n.attrs.get("role") == "button"]
    copy = [b for b in buttons if re.fullmatch(r"copy( prompt)?|copied", b.all_text().strip(), re.I) or "copy" in (b.attrs.get("aria-label") or "").lower()]
    bad_copy = 0
    prompts_seen = {}
    for b in copy:
        sib = [c for c in (b.parent.children if b.parent else []) if c is not b]
        has_pre = any(c.tag == "pre" for c in sib)
        if not has_pre:
            bad_copy += 1
        else:
            prompts_seen[id(b.parent)] = prompts_seen.get(id(b.parent), 0) + 1
    add("BLOCK", "copy-non-prompt", "copy buttons on anything but a prompt block (tables, charts, links, prose)", bad_copy)
    add("BLOCK", "copy-multiple", "more than one copy button on one prompt", sum(v - 1 for v in prompts_seen.values() if v > 1))
    if kind != "dashboard":
        other = [b for b in buttons if b not in copy]
        add("BLOCK", "buttons", "buttons that are not a prompt's Copy (fake actions, Share, Download)", len(other))
    add("WARN", "heading-links", "link icons inside headings",
        sum(1 for n in nodes if n.tag in ("h1", "h2", "h3") and any(d.tag in ("svg", "button") for d in n.walk() if d is not n)))

    # --- type ------------------------------------------------------------
    uses_inter = re.search(r"font-family\s*:[^;}]*inter|font\s*:[^;}]*inter", css_nofonts, re.I)
    loads_inter = re.search(r"@font-face\s*\{[^}]*font-family\s*:\s*['\"]?Inter", css, re.I) or "fonts.googleapis.com" in html
    if not uses_inter:
        add("BLOCK", "font-family", "body font is not Inter")
    elif not loads_inter:
        add("BLOCK", "font-load", "Inter is named but never loaded (@font-face or a Google Fonts link)")
    m = re.search(r"body\s*\{[^}]*?font(?:-size)?\s*:\s*(?:[0-9]{3}\s+)?(\d+(?:\.\d+)?)px", css_nofonts, re.I)
    if m and float(m.group(1)) < 17:
        add("BLOCK", "small-text", f"body text {m.group(1)}px; use 18-19px")
    elif not m:
        add("WARN", "text-size", "body font size not set in px; check it renders at 18-19px")

    # --- writing ---------------------------------------------------------
    hits = []
    for _, t_ in visible_text_nodes(root):
        for p in PHRASES:
            if re.search(p, t_, re.I):
                hits.append(re.search(p, t_, re.I).group(0))
    add("WARN", "phrases", "stock phrases: " + ", ".join(sorted(set(h.lower() for h in hits)))[:200], len(hits))
    lab = 0
    for n, t_ in visible_text_nodes(root):
        if any(re.match(r"\s*" + l, t_, re.I) for l in LABELS):
            lab += 1
    add("BLOCK", "boilerplate-labels", "boilerplate labels (Good to know, Key takeaways, At a glance, Prepared by, TL;DR)", lab)
    bold = 0
    for n in nodes:
        if n.tag in ("p", "li"):
            strongs = [c for c in n.children if c.tag in ("strong", "b")]
            if len(strongs) > 1:
                bold += 1
    add("WARN", "bold", "paragraphs with more than one bold run (bold only as a leading label)", bold)
    gen = [n.all_text() for n in nodes if n.tag in ("h2", "h3") and n.all_text().strip().lower().rstrip(":") in GENERIC_H]
    add("WARN", "generic-headings", "headings that do not state a point: " + ", ".join(gen)[:160], len(gen))
    h1 = [n for n in nodes if n.tag == "h1"]
    if len(h1) != 1:
        add("WARN", "h1", f"{len(h1)} h1 elements; use exactly one, stating the point")
    em_in_h1 = sum(1 for n in h1 for d in n.walk() if d.tag in ("em", "i", "span") and d is not n)
    add("BLOCK", "heading-flourish", "styled words inside the title (italic or coloured accent)", em_in_h1)
    return find


def render_checks(path):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [{"level": "WARN", "check": "render", "message": "playwright not installed: render checks skipped", "count": 1}]
    out = []
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(executable_path="/usr/bin/google-chrome")
        for w in (412, 1280):
            pg = b.new_page(viewport={"width": w, "height": 900}, locale="en-US")
            pg.goto(Path(path).resolve().as_uri())
            pg.wait_for_timeout(300)
            size = pg.evaluate("parseFloat(getComputedStyle(document.querySelector('main p, p') || document.body).fontSize)")
            over = pg.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
            if size < (16 if w == 412 else 17):
                out.append({"level": "BLOCK", "check": "render-text-size", "message": f"body text {size}px at {w}px wide", "count": 1})
            if over:
                out.append({"level": "BLOCK", "check": "render-overflow", "message": f"horizontal scroll at {w}px wide", "count": 1})
            pg.close()
        b.close()
    return out


def main():
    ap = argparse.ArgumentParser(description="PyMC Labs pre-publish lint")
    ap.add_argument("html", nargs="+")
    ap.add_argument("--kind", choices=["page", "dashboard"], default="page")
    ap.add_argument("--render", action="store_true", help="also check computed text size and phone overflow")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    worst = 0
    report = {}
    for f in a.html:
        res = lint(Path(f).read_text(encoding="utf-8", errors="replace"), a.kind, Path(f).parent)
        if a.render:
            res += render_checks(f)
        report[f] = res
        worst = max(worst, 1 if any(r["level"] == "BLOCK" for r in res) else 0)
    if a.json:
        print(json.dumps(report, indent=1))
    else:
        for f, res in report.items():
            blocks = sum(1 for r in res if r["level"] == "BLOCK")
            print(f"{f}: {blocks} block, {len(res) - blocks} warn")
            for r in sorted(res, key=lambda r: r["level"]):
                print(f"  {r['level']:5} {r['check']}: {r['message']}" + (f" (x{r['count']})" if r["count"] > 1 else ""))
    sys.exit(worst)


if __name__ == "__main__":
    main()
