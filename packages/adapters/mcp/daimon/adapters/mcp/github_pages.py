"""GitHub connection pages with a compact, responsive picker."""

from __future__ import annotations

import html

from daimon.adapters.mcp.branded_pages import branded_page
from starlette.responses import HTMLResponse


def github_page(
    *,
    title: str,
    body_html: str,
    status: int = 200,
    error: bool = False,
    kind: str = "status",
) -> HTMLResponse:
    """Render one server-generated GitHub page with Daimon branding."""
    return branded_page(
        title=title,
        state_bar=" status-bar--rose" if error else "",
        body_html=(
            f'<div class="gh-flow gh-flow--{html.escape(kind, quote=True)}">'
            f"<h1>{html.escape(title)}</h1>{body_html}</div>"
        ),
        status=status,
        context="GitHub",
        wide=kind == "picker",
        card=kind != "picker",
    )
