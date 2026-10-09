"""GitHub connection pages with a compact, responsive picker."""

from __future__ import annotations

import html

from daimon.adapters.mcp.branded_pages import branded_page
from daimon.adapters.mcp.web_icons import icon
from starlette.responses import HTMLResponse


def github_page(
    *,
    title: str,
    body_html: str,
    status: int = 200,
    error: bool = False,
    kind: str = "status",
    heading_icon: bool = False,
) -> HTMLResponse:
    """Render one server-generated GitHub page with Daimon branding."""
    heading = (
        f'<h1 class="gh-title-marked">{icon("github")}<span>{html.escape(title)}</span></h1>'
        if heading_icon
        else f"<h1>{html.escape(title)}</h1>"
    )
    return branded_page(
        title=title,
        state_bar=" status-bar--rose" if error else "",
        body_html=f'<div class="gh-flow gh-flow--{html.escape(kind, quote=True)}">'
        f"{heading}{body_html}</div>",
        status=status,
        context="GitHub",
        wide=kind == "picker",
        card=kind != "picker",
    )
