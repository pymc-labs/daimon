#!/usr/bin/env python3
"""Pre-publish lint for PyMC Labs HTML artifacts. Run it before publishing any HTML.

    python3 publish_lint.py page.html [--kind page|dashboard] [--render] [--json]

Exit codes: 0 clean (WARN findings may remain), 1 at least one BLOCK finding,
2 the lint could not run (missing file, unreadable input). Fix every BLOCK and run
again; fix WARN findings too unless they are false positives, and say so in the
send-readiness note.

Standard library only. --render (needs Playwright) also checks the computed
body text size and horizontal overflow at 412px and 1280px.

It reads pages and dashboards you author. It does not lint marimo exports
(their interface is drawn by JavaScript), PDFs or slides: those get their look
from the bundled templates.

--kind dashboard allows what an interactive dashboard needs (tabs, buttons,
selects, sliders); everything else is checked the same way.

Emoji means pictographs (U+1F300 and up, plus a few common ones such as the
check-mark and warning signs in colour). Plain symbols like ✓ ✗ ★ → are allowed.
"""
import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
# end tags HTML lets you omit: a new sibling of these closes the open one
IMPLIED = {"p": {"p", "div", "ul", "ol", "table", "h1", "h2", "h3", "h4", "pre", "figure", "section", "header", "footer"},
           "li": {"li"}, "td": {"td", "th", "tr"}, "th": {"td", "th", "tr"}, "tr": {"tr"},
           "option": {"option"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"}}

EMOJI = re.compile("[\U0001F300-\U0001FAFF\U0001F1E6-\U0001F1FF⭐✅❌⚠⚡✨❤️]")
# Claude-isms and stock phrasing: BLOCK
PHRASES = [
    r"\bnot just\b", r"\bnot only\b[^.]{0,80}\bbut\b", r"here(?:'| i)s the thing", r"let'?s dive in", r"\bdive (?:into|deep)\b",
    r"\bin today'?s\b", r"\bgame[- ]changer\b", r"\bi hope this helps\b", r"\bit'?s worth noting\b", r"\bdelve\b",
    r"\btapestry\b", r"\bwithout further ado\b", r"\bin conclusion\b", r"\bnavigat(?:e|ing) the (?:complex|landscape)",
    r"\bthe short answer\b", r"\blet'?s break (?:it|this) down\b", r"\bbuckle up\b", r"\bin a nutshell\b",
]
# softer marketing words with legitimate uses ("robust standard errors"): WARN
SOFT = [r"\bseamless(?:ly)?\b", r"\bunlock(?:s|ing)?\b", r"\brobust\b", r"\bleverag(?:e|ing)\b", r"\bat the end of the day\b",
        r"\bempower(?:s|ing)?\b", r"\bcutting[- ]edge\b"]
LABELS = [r"good to know", r"key takeaways?", r"at a glance", r"prepared by\b", r"tl;?dr", r"pro tip", r"heads up"]
GENERIC_H = {"results", "overview", "summary", "method", "methods", "background", "next steps", "introduction",
             "conclusion", "details", "context", "findings", "notes", "about", "how sure we are", "what we found",
             "what we need from you", "key numbers", "recommendations", "recommended next steps"}
TOKEN = {
    "pills": re.compile(r"^(?:.*[-_])?(?:badge|pill|chip)s?(?:[-_].*)?$", re.I),
    "cards": re.compile(r"^(?:.*[-_])?(?:card|tile|kpi)s?(?:[-_].*)?$", re.I),
    "hero": re.compile(r"^(?:.*[-_])?(?:hero|banner|masthead|jumbotron)(?:[-_].*)?$", re.I),
    "eyebrow": re.compile(r"^(?:.*[-_])?(?:eyebrow|kicker|overline)(?:[-_].*)?$", re.I),
    "callout": re.compile(r"^(?:.*[-_])?(?:callout|admonition|alert)s?(?:[-_].*)?$", re.I),
    "sidebar": re.compile(r"^(?:.*[-_])?(?:sidebar|sidenav|side-nav|toc)(?:[-_].*)?$", re.I),
}
NAMED_DARK = {"black", "navy", "midnightblue", "darkslategray", "darkslategrey"}
DARK_VARS = re.compile(r"var\(\s*--(?:navy(?:-deep|-header)?|ink|dark[\w-]*)\s*\)", re.I)


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children, self.text = tag, dict(attrs), parent, [], []

    def cls(self):
        return self.attrs.get("class") or ""

    def tokens(self):
        return self.cls().split()

    def walk(self):
        stack = [self]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed(n.children))

    def all_text(self):
        return " ".join(t for n in self.walk() for t in n.text).strip()

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent

    def where(self):
        bits = [self.tag]
        if self.attrs.get("class"):
            bits.append("." + ".".join(self.tokens()[:3]))
        t = self.all_text()[:50]
        return "".join(bits) + (f' "{t}"' if t else "")


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root
        self.css = []
        self.blocks = []  # <style> contents and linked stylesheets, without inline styles
        self.inline = []

    def handle_starttag(self, tag, attrs):
        # close an open element whose end tag HTML lets you omit
        n = self.cur
        while n is not self.root and n.tag in IMPLIED and tag in IMPLIED[n.tag]:
            self.cur = n = n.parent
        node = Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if node.attrs.get("style"):
            self.css.append(node.attrs["style"])
            self.inline.append(node.attrs["style"])
        if tag not in VOID:
            self.cur = node

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
            self.blocks.append(data)
        elif self.cur.tag != "script" and data.strip():
            self.cur.text.append(data.strip())


def _lum(color):
    """Relative luminance of a CSS colour literal, or None."""
    c = color.strip().lower()
    if c in NAMED_DARK:
        return 0.0
    m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})(?:[0-9a-f]{2})?", c)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    else:
        m = re.fullmatch(r"rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)(?:[ ,/]+([\d.]+%?))?\s*\)", c)
        if not m:
            return None
        if m.group(4) is not None:
            a = float(m.group(4).rstrip("%")) / (100 if m.group(4).endswith("%") else 1)
            if a < 0.6:
                return None  # a translucent tint, not a dark band
        rgb = [int(m.group(i)) / 255 for i in (1, 2, 3)]
    lin = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def dark_backgrounds(css, inline):
    hits = []
    for sel, body in re.findall(r"([^{}]*)\{([^{}]*)\}", css) + [("(inline style)", s) for s in inline]:
        for decl in re.findall(r"background(?:-color)?\s*:\s*([^;]+)", body, re.I):
            sel_ = sel.strip().split(",")[0][-40:] or "(inline)"
            if re.search(r"\b(pre|code)\b", sel_):
                continue
            if DARK_VARS.search(decl):
                hits.append(sel_); continue
            plain = re.sub(r"var\([^)]*\)|url\([^)]*\)", " ", decl)
            for col in re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|\b[a-zA-Z]+\b", plain):
                lum = _lum(col)
                if lum is not None and lum < 0.05:
                    hits.append(sel_); break
    return hits


def visible_text_nodes(root):
    for n in root.walk():
        if n.tag in ("script", "style", "head", "title") or any(a.tag in ("script", "style", "head") for a in n.ancestors()):
            continue
        for t in n.text:
            yield n, t


def lint(html, kind="page", base=None):
    t = Tree()
    t.feed(html)
    root = t.root
    if base is not None:  # local stylesheets count as the page's CSS
        for n in root.walk():
            href = n.attrs.get("href") or ""
            if n.tag == "link" and "stylesheet" in (n.attrs.get("rel") or "") and not re.match(r"^(https?:)?//", href):
                f = Path(base) / href
                if f.exists():
                    sheet = f.read_text(encoding="utf-8", errors="replace")
                    t.css.append(sheet)
                    t.blocks.append(sheet)
    css = "\n".join(t.css)
    css_nofonts = re.sub(r"@font-face\s*\{[^}]*\}", "", css)
    css_nofonts = re.sub(r"url\(data:[^)]*\)", "url()", css_nofonts)
    nodes = list(root.walk())
    texts = list(visible_text_nodes(root))
    find = []

    def add(level, cid, msg, items=None, n=None):
        count = n if n is not None else (len(items) if items is not None else 1)
        if count:
            where = ""
            if items:
                first = items[0]
                where = " — first: " + (first.where() if isinstance(first, Node) else str(first))[:90]
            find.append({"level": level, "check": cid, "message": msg + where, "count": count})

    def css_hits(pattern):
        return [m.group(0)[:60] for m in re.finditer(pattern, css_nofonts, re.I)]

    def token_hits(name):
        return [n for n in nodes if any(TOKEN[name].match(tok) for tok in n.tokens())]

    # --- hidden content --------------------------------------------------
    add("BLOCK", "accordion", "accordions hide content: show it, or cut it",
        [n for n in nodes if n.tag in ("details", "summary") or "aria-expanded" in n.attrs])
    controls = [n for n in nodes if n.tag in ("button", "a", "summary") or n.attrs.get("role") == "button"]
    add("BLOCK", "expand-collapse", "expand / collapse / show-more controls",
        [n for n in controls if re.search(r"\b(expand|collapse) all\b|\bshow (more|all|less)\b|\bread more\b", n.all_text(), re.I)])

    # --- navigation ------------------------------------------------------
    add("BLOCK", "sticky", "sticky or fixed positioning (sticky nav, floating bars)", css_hits(r"position\s*:\s*(sticky|fixed)"))
    add("BLOCK", "sidebar", "sidebar or table-of-contents column", [n for n in nodes if n.tag == "aside"] + token_hits("sidebar"))
    if kind != "dashboard":
        add("BLOCK", "tabs", "tabs or tab chips on a document", [n for n in nodes if n.tag == "nav" or n.attrs.get("role") == "tablist"])

    # --- decoration ------------------------------------------------------
    add("BLOCK", "gradient", "gradients",
        css_hits(r"(linear|radial|conic)-gradient\(") + [n for n in nodes if n.tag.lower() in ("lineargradient", "radialgradient")])
    add("BLOCK", "shadow", "shadows",
        [m for m in css_hits(r"(box-shadow|text-shadow)\s*:\s*[^;}\"]*") if not re.search(r":\s*none", m)] + css_hits(r"drop-shadow\("))
    add("BLOCK", "dark-band", "dark header band or dark section background", dark_backgrounds(
        re.sub(r"(?s)/\*.*?\*/|@font-face\s*\{[^}]*\}", "", "\n".join(t.blocks)), t.inline))
    add("BLOCK", "hero", "hero / banner block", token_hits("hero"))

    def svg_wide(n):
        w = re.sub(r"[^\d.]", "", n.attrs.get("width", "") or "")
        if w and float(w or 0) > 48:
            return True
        vb = (n.attrs.get("viewbox") or "").split()
        if len(vb) == 4 and not w:
            try:
                return float(vb[2]) > 48
            except ValueError:
                return False
        return False
    svgs = [n for n in nodes if n.tag == "svg" and not any(a.tag in ("button", "a", "figure") for a in n.ancestors())]
    add("BLOCK", "decorative-svg", "inline SVG art outside a figure (hero art, mock UI, illustrations)",
        [n for n in svgs if svg_wide(n) or any(TOKEN["hero"].match(x) or "art" in x for x in n.tokens())])
    add("BLOCK", "serif-accent", "serif font (one family only: Inter, plus JetBrains Mono for code)",
        css_hits(r"font(-family)?\s*:[^;}]*\b(georgia|times|garamond|playfair|(?<!sans-)serif)\b"))
    add("BLOCK", "emoji", "emoji", [t_ for _, t_ in texts if EMOJI.search(t_)])

    # --- components ------------------------------------------------------
    add("BLOCK", "pills", "pills, badges or chips",
        token_hits("pills") + css_hits(r"border-radius\s*:\s*(999|9999|100)[0-9]*(px|%)?|border-radius\s*:\s*50%"))
    add("BLOCK", "cards", "cards or key-number tiles", token_hits("cards"))

    def is_icon(d):
        if d.tag in ("svg", "i"):
            return True
        if d.tag == "img" and "mark" not in d.tokens():
            w = re.sub(r"\D", "", d.attrs.get("width", "") or "")
            return bool(w) and int(w) <= 48
        return False
    icon_grids = []
    for n in nodes:
        kids = [c for c in n.children if c.tag not in ("script", "style")]
        if len(kids) < 3:
            continue
        lead = 0
        for c in kids:
            if c.tag == "figure" or any(d.tag == "figure" for d in c.walk()):
                continue
            first = next((d for d in c.walk() if d is not c and (d.tag in ("svg", "i", "img") or d.text)), None)
            if first is not None and is_icon(first):
                lead += 1
        if lead >= 3:
            icon_grids.append(n)
    add("BLOCK", "icon-cards", "grid of items that each start with an icon", icon_grids)
    radius = [float(x) for x in re.findall(r"border-radius\s*:\s*(\d+(?:\.\d+)?)px", css_nofonts, re.I)]
    add("WARN", "rounded", "rounded corners over 4px (rounded-card look)", n=sum(1 for r in radius if 4 < r < 999))
    add("BLOCK", "callout", "callout / admonition boxes: say it in a sentence where it applies",
        token_hits("callout") + [n for n in nodes if n.tag == "blockquote"])
    add("WARN", "eyebrow", "eyebrow / kicker label above a heading",
        token_hits("eyebrow") + css_hits(r"text-transform\s*:\s*uppercase"))

    # --- buttons ---------------------------------------------------------
    buttons = [n for n in nodes if n.tag == "button" or n.attrs.get("role") == "button"]
    copy = [b for b in buttons if re.fullmatch(r"copy( prompt)?|copied", b.all_text().strip(), re.I)
            or "copy" in (b.attrs.get("aria-label") or "").lower()]
    bad_copy, per_prompt = [], {}
    for b in copy:
        sib = [c for c in (b.parent.children if b.parent else []) if c is not b]
        if any(c.tag == "pre" for c in sib):
            per_prompt.setdefault(id(b.parent), []).append(b)
        else:
            bad_copy.append(b)
    add("BLOCK", "copy-non-prompt", "copy buttons on anything but a prompt block (tables, charts, links, prose)", bad_copy)
    add("BLOCK", "copy-multiple", "more than one copy button on one prompt", [b for v in per_prompt.values() for b in v[1:]])
    if kind != "dashboard":
        add("BLOCK", "buttons", "buttons that are not a prompt's Copy (fake actions, Start, Share, Download)",
            [b for b in buttons if b not in copy])
    add("WARN", "heading-links", "link icons inside headings",
        [n for n in nodes if n.tag in ("h1", "h2", "h3") and any(d.tag in ("svg", "button") for d in n.walk() if d is not n)])

    # --- type ------------------------------------------------------------
    uses_inter = re.search(r"font(-family)?\s*:[^;}]*inter", css_nofonts, re.I)
    loads_inter = re.search(r"@font-face\s*\{[^}]*font-family\s*:\s*['\"]?Inter", css, re.I) or "fonts.googleapis.com" in html
    if not uses_inter:
        add("BLOCK", "font-family", "body font is not Inter")
    elif not loads_inter:
        add("BLOCK", "font-load", "Inter is named but never loaded (@font-face or a Google Fonts link)")
    sizes = re.findall(r"(?:^|[}\s,])body\s*\{[^}]*?font(?:-size)?\s*:\s*(?:[a-z]+\s+)*?(?:[0-9]{3}\s+)?(\d+(?:\.\d+)?)px",
                       css_nofonts, re.I)
    if sizes and float(sizes[-1]) < 17:
        add("BLOCK", "small-text", f"body text {sizes[-1]}px; use 18-19px")
    elif not sizes:
        add("WARN", "text-size", "body font size not set in px; check it renders at 18-19px (--render checks it)")

    # --- writing ---------------------------------------------------------
    def phrase_hits(pats):
        out = []
        for _, t_ in texts:
            for p in pats:
                m = re.search(p, t_, re.I)
                if m:
                    out.append(f'"{m.group(0)}" in "{t_[:60]}"')
        return out
    add("BLOCK", "phrases", "stock phrasing (Claude-isms): rewrite plainly", phrase_hits(PHRASES))
    add("WARN", "soft-phrases", "marketing words: keep only if literal (e.g. robust standard errors)", phrase_hits(SOFT))
    add("BLOCK", "boilerplate-labels", "boilerplate labels (Good to know, Key takeaways, At a glance, Prepared by, TL;DR)",
        [t_ for _, t_ in texts if any(re.match(r"\s*" + lab, t_, re.I) for lab in LABELS)])
    add("BLOCK", "bold", "more than one bold run in a paragraph (bold only as a leading label)",
        [n for n in nodes if n.tag in ("p", "li") and sum(1 for c in n.children if c.tag in ("strong", "b")) > 1])
    add("WARN", "generic-headings", "headings that do not state a point",
        [n for n in nodes if n.tag in ("h2", "h3") and n.all_text().strip().lower().rstrip(":?") in GENERIC_H])
    h1 = [n for n in nodes if n.tag == "h1"]
    if len(h1) != 1:
        add("WARN", "h1", f"{len(h1)} h1 elements; use exactly one, stating the point")
    add("BLOCK", "heading-flourish", "styled words inside the title (italic or coloured accent)",
        [d for n in h1 for d in n.walk() if d.tag in ("em", "i", "span") and d is not n])
    return find


def render(path):
    """Computed body text size and horizontal overflow at phone and desktop width."""
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
            pg.wait_for_timeout(1500)
            size = pg.evaluate("(() => { const ps = [...document.querySelectorAll('p')].filter(p => p.innerText.length > 40);"
                               " return parseFloat(getComputedStyle(ps[0] || document.body).fontSize); })()")
            if size < (16 if w == 412 else 17):
                out.append({"level": "BLOCK", "check": "render-text-size", "message": f"body text {size}px at {w}px wide", "count": 1})
            if pg.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"):
                out.append({"level": "BLOCK", "check": "render-overflow", "message": f"horizontal scroll at {w}px wide", "count": 1})
            pg.close()
        b.close()
    return out


def main():
    ap = argparse.ArgumentParser(description="PyMC Labs pre-publish lint")
    ap.add_argument("html", nargs="+")
    ap.add_argument("--kind", choices=["page", "dashboard"], default="page")
    ap.add_argument("--render", action="store_true", help="also check computed text size and phone overflow (Playwright)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    report = {}
    for f in a.html:
        try:
            text = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"cannot read {f}: {e}", file=sys.stderr)
            sys.exit(2)
        res = lint(text, a.kind, Path(f).parent)
        if a.render:
            res += render(f)
        report[f] = res
    worst = 1 if any(r["level"] == "BLOCK" for res in report.values() for r in res) else 0
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
