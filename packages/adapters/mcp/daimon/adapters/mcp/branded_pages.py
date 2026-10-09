"""Shared branded page rendering for browser authorization flows."""

from __future__ import annotations

from daimon.adapters.mcp.web_shell import render_page
from starlette.responses import HTMLResponse


def branded_page(
    *,
    title: str,
    state_bar: str,
    body_html: str,
    status: int = 200,
    context: str | None = None,
    wide: bool = False,
    card: bool = True,
) -> HTMLResponse:
    return render_page(
        title=title,
        body_html=body_html,
        status=status,
        context=context,
        error="rose" in state_bar or status >= 400,
        wide=wide,
        card=card,
    )
