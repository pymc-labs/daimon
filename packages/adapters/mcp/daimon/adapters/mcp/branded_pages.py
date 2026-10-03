"""Shared branded page rendering for browser authorization flows."""

from __future__ import annotations

from daimon.adapters.mcp.oauth_slack import _page  # pyright: ignore[reportPrivateUsage]
from starlette.responses import HTMLResponse


def branded_page(*, title: str, state_bar: str, body_html: str, status: int = 200) -> HTMLResponse:
    return _page(title=title, state_bar=state_bar, body_html=body_html, status=status)
