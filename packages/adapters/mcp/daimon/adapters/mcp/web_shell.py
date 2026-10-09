"""One server-rendered shell for MCP browser pages."""

from __future__ import annotations

import html
from pathlib import Path

from starlette.responses import HTMLResponse

STATIC_DIR = Path(__file__).with_name("static")


def render_page(
    *,
    title: str,
    body_html: str,
    status: int = 200,
    context: str | None = None,
    error: bool = False,
    wide: bool = False,
    card: bool = True,
) -> HTMLResponse:
    """Render trusted, already escaped inner HTML inside the shared page shell."""
    safe_title = html.escape(title)
    badge = (
        f'<span data-slot="badge" class="web-context">{html.escape(context)}</span>'
        if context
        else ""
    )
    content = (
        f'<section data-slot="card" class="web-card{" web-card--error" if error else ""}">'
        f"{body_html}</section>"
        if card
        else body_html
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="only light">
<title>Daimon: {safe_title}</title>
<link rel="stylesheet" href="/web/web.css"></head>
<body><header class="web-header"><div class="web-header-inner">
<div class="web-brand"><img class="web-mark" src="/web/daimon.png" alt="Daimon">
<span><strong>Daimon</strong><small>by PyMC Labs</small></span></div>{badge}
</div></header>
<main class="web-main{" web-main--wide" if wide else ""}">{content}</main>
</body></html>"""
    response = HTMLResponse(document, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
