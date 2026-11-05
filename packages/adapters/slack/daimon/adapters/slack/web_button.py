"""The Block Kit button that opens a web app page."""

from __future__ import annotations

import uuid
from typing import Any

from daimon.core.config import WebSettings
from daimon.core.web_links import OPEN_IN_WEB, panel_links


def open_in_web_button(url: str) -> dict[str, Any]:
    """A url button; Slack opens it in the browser and a click needs no handler."""
    return {
        "type": "button",
        "action_id": "open_in_web",
        "text": {"type": "plain_text", "text": OPEN_IN_WEB},
        "url": url,
    }


def spend_url(web: WebSettings | None, tenant_id: uuid.UUID) -> str | None:
    """The workspace's spend page, or None with no web app. Any member may open it."""
    links = panel_links(web, tenant_id, admin=False)
    return None if links is None else links.spend()
