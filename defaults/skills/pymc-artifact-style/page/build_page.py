#!/usr/bin/env python3
"""Build a single-file PyMC Labs page (guide, report, one-pager, FAQ) from Markdown.

    python3 build_page.py content.md -o page.html [--footer "..."] [--fonts embed|google]

Standard library only. The output is one HTML file: fonts and the wordmark are
embedded, images referenced in the Markdown are inlined, so it renders offline.

Markdown subset (the whole vocabulary the house style needs):

    # Title that states the point          -> h1 (one per page)
    first paragraph after the h1           -> the lede (larger text)
    ## Heading that states the point       -> h2;  ### -> h3
    plain paragraphs, - lists, 1. lists, ---
    | a | b |  pipe tables                  -> rules-only table; numeric cells right-aligned
    ```prompt ... ```                      -> prompt block with ONE Copy button
    ```python ... ``` (any other fence)    -> plain code block, no button
    ![Caption](figure.png)                 -> figure, image inlined, caption under it
    **bold**  *italic*  `code`  [text](url)

Anything outside this subset (cards, badges, callouts, accordions, sidebars) is
deliberately not supported: the style bans it.
"""
import argparse
import base64
import datetime as _dt
import html
import mimetypes
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
FONTS = [("Inter", 400, "normal", "Inter-Regular.ttf"), ("Inter", 600, "normal", "Inter-SemiBold.ttf"),
         ("JetBrains Mono", 400, "normal", "JetBrainsMono-Regular.ttf")]
GOOGLE = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
          '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
          '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600'
          '&family=JetBrains+Mono&display=swap" rel="stylesheet">')
COPY_JS = ("<script>document.querySelectorAll('.prompt button').forEach(b=>b.onclick=()=>{"
           "navigator.clipboard.writeText(b.previousElementSibling.innerText);"
           "b.textContent='Copied';setTimeout(()=>b.textContent='Copy',1500);});</script>")
NUMERIC = re.compile(r"^[\s$€£~<>≈±+\-−]*[\d.,]+[\d.,%×x()\s–\-−kKmMbB]*$")


def b64(path):
    return base64.b64encode(Path(path).read_bytes()).decode()


def font_css():
    out = []
    for fam, w, st, f in FONTS:
        out.append(f"@font-face{{font-family:'{fam}';font-weight:{w};font-style:{st};font-display:swap;"
                   f"src:url(data:font/ttf;base64,{b64(SKILL / 'fonts' / f)}) format('truetype');}}")
    return "".join(out)


URL = r"((?:[^()\s]|\([^()\s]*\))+)"  # one level of balanced parentheses, e.g. wiki links


def safe_url(url, images=False):
    """Allow http(s), mailto, #anchors and relative paths (and data:image for images); else '#'."""
    u = url.strip()
    if re.match(r"^(https?:|mailto:|#|/|\.{0,2}/|[\w.-]+(?:/|$|#|\?))", u, re.I) and not re.match(r"^\s*(javascript|vbscript|data):", u, re.I):
        return u
    if images and re.match(r"^data:image/(png|jpe?g|gif|webp|svg\+xml);", u, re.I):
        return u
    return "#"


def inline(text, base):
    """Escape, then apply the inline Markdown subset. Code spans and links are pulled out
    first so their contents are escaped exactly once and never re-parsed as emphasis."""
    keep = []

    def stash(html_text):
        keep.append(html_text)
        return f"\x00{len(keep) - 1}\x00"

    text = re.sub(r"`([^`]+)`", lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = re.sub(r"\[([^\]]+)\]\(" + URL + r"\)",
                  lambda m: stash(f'<a href="{html.escape(safe_url(m.group(2)))}">')
                  + m.group(1) + stash("</a>"), text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)


def image(src, caption, base):
    src = re.sub(r'\s+"[^"]*"$', "", src.strip())  # drop a Markdown title: ![a](b.png "title")
    if re.match(r"^(https?:|data:)", src, re.I):
        src = safe_url(src, images=True)
    else:
        p = base / src
        if not p.exists():
            sys.exit(f"image not found: {p}")
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        src = f"data:{mime};base64,{b64(p)}"
    cap = f"<figcaption>{inline(caption, base)}</figcaption>" if caption else ""
    return f'<figure><img src="{html.escape(src)}" alt="{html.escape(caption)}">{cap}</figure>'


def split_row(row):
    """Split a pipe-table row, honouring \\| escapes and pipes inside `code`."""
    cells, cur, code, i, r = [], "", False, 0, row.strip()
    if r.startswith("|"):
        r = r[1:]
    if r.endswith("|") and not r.endswith("\\|"):
        r = r[:-1]
    while i < len(r):
        ch = r[i]
        if ch == "\\" and i + 1 < len(r) and r[i + 1] == "|":
            cur += "|"; i += 2; continue
        if ch == "`":
            code = not code
        if ch == "|" and not code:
            cells.append(cur.strip()); cur = ""
        else:
            cur += ch
        i += 1
    cells.append(cur.strip())
    return cells


def table(rows, base):
    cells = [split_row(r) for r in rows]
    head = cells[0]
    has_rule = len(rows) > 1 and set(rows[1].replace("|", "").strip()) <= set("-: ")
    body = cells[2:] if has_rule else cells[1:]
    num = []
    for i in range(len(head)):
        vals = [r[i] for r in body if i < len(r) and r[i]]
        num.append(bool(vals) and all(NUMERIC.match(v) for v in vals))
    ncls = ' class="n"'
    th = "".join(f"<th{ncls if num[i] else ''}>{inline(h, base)}</th>" for i, h in enumerate(head))
    trs = "".join("<tr>" + "".join(f"<td{ncls if i < len(num) and num[i] else ''}>{inline(c, base)}</td>"
                                   for i, c in enumerate(r)) + "</tr>" for r in body)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def render(md, base):
    lines = md.replace("\r\n", "\n").split("\n")
    out, i, seen_h1, lede_done, para = [], 0, False, False, []

    def flush():
        nonlocal para, lede_done
        if para:
            text = inline(" ".join(para), base)
            if seen_h1 and not lede_done and not any(o.startswith("<h2") for o in out):
                out.append(f'<p class="lede">{text}</p>'); lede_done = True
            else:
                out.append(f"<p>{text}</p>")
            para = []

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if s.startswith("```"):
            flush()
            lang = s[3:].strip()
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            code = html.escape("\n".join(lines[i + 1:j]))
            out.append(f'<div class="prompt"><pre>{code}</pre><button type="button">Copy</button></div>'
                       if lang == "prompt" else f"<pre><code>{code}</code></pre>")
            i = j + 1; continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush()
            n = min(len(m.group(1)), 3); seen_h1 = seen_h1 or n == 1
            out.append(f"<h{n}>{inline(m.group(2), base)}</h{n}>"); i += 1; continue
        if s == "---":
            flush(); out.append("<hr>"); i += 1; continue
        m = re.match(r"^!\[([^\]]*)\]\((.+)\)$", s)
        if m:
            flush(); out.append(image(m.group(2), m.group(1), base)); i += 1; continue
        if s.startswith("|"):
            flush(); j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            out.append(table(lines[i:j], base)); i = j; continue
        m = re.match(r"^([-*]|\d+[.)])\s+(.*)$", s)
        if m:
            flush(); tag = "ul" if m.group(1) in "-*" else "ol"; items = []
            while i < len(lines):
                m2 = re.match(r"^([-*]|\d+[.)])\s+(.*)$", lines[i].strip())
                if m2 and ("ul" if m2.group(1) in "-*" else "ol") != tag:
                    break
                if m2:
                    items.append(m2.group(2))
                elif lines[i].startswith("  ") and lines[i].strip() and items:
                    items[-1] += " " + lines[i].strip()
                else:
                    break
                i += 1
            out.append(f"<{tag}>" + "".join(f"<li>{inline(t, base)}</li>" for t in items) + f"</{tag}>"); continue
        if not s:
            flush(); i += 1; continue
        para.append(s); i += 1
    flush()
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("content")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--footer", default=None, help="one line; default 'PyMC Labs, <Month YYYY>'")
    ap.add_argument("--fonts", choices=["embed", "google"], default="embed")
    a = ap.parse_args()
    src = Path(a.content)
    md = src.read_text(encoding="utf-8")
    body = render(md, src.parent)
    t = re.search(r"<h1>(.*?)</h1>", body)
    title = re.sub(r"<[^>]+>", "", t.group(1)) if t else "PyMC Labs"
    footer = a.footer if a.footer is not None else f"PyMC Labs, {_dt.date.today():%B %Y}."
    fonts = f"<style>{font_css()}</style>" if a.fonts == "embed" else GOOGLE
    has_prompt = 'class="prompt"' in body
    page = ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{title}</title>{fonts}<style>{(HERE / 'page.css').read_text()}</style></head><body><main>\n"
            f'<img class="mark" src="data:image/png;base64,{b64(SKILL / "assets" / "pymc-labs-logo-dark.png")}" alt="PyMC Labs">\n'
            f"{body}\n<hr><footer>{inline(footer, src.parent)}</footer>\n</main>"
            + (COPY_JS if has_prompt else "") + "</body></html>\n")
    Path(a.out).write_text(page, encoding="utf-8")
    print(f"wrote {a.out} ({len(page) / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
