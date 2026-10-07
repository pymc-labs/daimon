"""Opening and replacing modals off a click, without letting Slack's refusal escape.

A ``trigger_id`` lives 3 seconds, so a handler that has checks to run opens
its modal first (the form itself, or a "Checking…" notice) and replaces it
once it has decided. A refusal from Slack (an expired trigger, a hiccup) is
returned to the caller to recover from, never raised into a spawned task
where it would vanish and leave the click looking dead.
"""

from __future__ import annotations

from typing import Any, cast

import structlog
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

__all__ = ["notice_modal", "open_modal", "update_modal"]

log = structlog.get_logger()


def notice_modal(*, title: str, text: str) -> dict[str, Any]:
    """A no-submit modal that only says something: "Checking…" or why not."""
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": title},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    }


async def open_modal(
    client: AsyncWebClient, *, trigger_id: str, view: dict[str, Any]
) -> str | None:
    """``views.open``, returning the view id, or None when Slack refused it."""
    try:
        resp = await client.views_open(trigger_id=trigger_id, view=view)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    except SlackApiError as err:
        log.info(
            "slack.views_open_failed",
            error=str(err.response.get("error", "slack_api_error")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
        )
        return None
    opened = cast("dict[str, Any]", resp.get("view") or {})  # pyright: ignore[reportUnknownMemberType]  # slack_sdk response is dict-like
    return str(opened.get("id") or "") or None


async def update_modal(client: AsyncWebClient, *, view_id: str, view: dict[str, Any]) -> bool:
    """``views.update`` on a modal this handler opened; False when Slack refused it."""
    try:
        await client.views_update(view_id=view_id, view=view)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    except SlackApiError as err:
        log.info(
            "slack.views_update_failed",
            error=str(err.response.get("error", "slack_api_error")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
        )
        return False
    return True
