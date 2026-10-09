"""Shared branded page rendering for browser authorization flows."""

from __future__ import annotations

from daimon.adapters.mcp.oauth_slack import render_branded_page
from starlette.responses import HTMLResponse


def branded_page(*, title: str, state_bar: str, body_html: str, status: int = 200) -> HTMLResponse:
    response = render_branded_page(
        title=title, state_bar=state_bar, body_html=body_html, status=status
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
