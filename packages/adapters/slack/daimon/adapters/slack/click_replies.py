"""Answering a click on an answer: modals and ephemerals, without letting a failure escape.

A ``trigger_id`` lives 3 seconds, so a handler that has checks to run opens
its modal first (the form itself, or a "Checking…" notice) and replaces it
once it has decided. A refusal from Slack (an expired trigger, a hiccup) or a
transport failure is returned to the caller to recover from, never raised
into a spawned task where it would vanish and leave the click looking dead.

An ephemeral is posted into the answer's thread only when the answer is in
one: Slack drops a threaded ephemeral whose ``thread_ts`` names a message
with no thread (a top-level answer, most DM answers).
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import aiohttp
import structlog
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

__all__ = [
    "CLICK_REPLY_ERRORS",
    "error_name",
    "notice_modal",
    "open_modal",
    "post_ephemeral",
    "update_modal",
]

log = structlog.get_logger()

CLICK_REPLY_ERRORS = (SlackApiError, aiohttp.ClientError, asyncio.TimeoutError)


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
    except CLICK_REPLY_ERRORS as err:
        log.info(
            "slack.views_open_failed",
            error=error_name(err),
        )
        return None
    opened = cast("dict[str, Any]", resp.get("view") or {})  # pyright: ignore[reportUnknownMemberType]  # slack_sdk response is dict-like
    return str(opened.get("id") or "") or None


async def update_modal(client: AsyncWebClient, *, view_id: str, view: dict[str, Any]) -> bool:
    """``views.update`` on a modal this handler opened; False when Slack refused it."""
    try:
        await client.views_update(view_id=view_id, view=view)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    except CLICK_REPLY_ERRORS as err:
        log.info(
            "slack.views_update_failed",
            error=error_name(err),
        )
        return False
    return True


async def post_ephemeral(
    client: AsyncWebClient,
    *,
    channel_id: str,
    user_id: str,
    thread_ts: str,
    message_ts: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
) -> None:
    """A message only ``user_id`` sees, beside the answer at ``message_ts``.

    ``thread_ts`` is the answer's place key, which falls back to the answer's
    own ts; it names a thread only when it differs from ``message_ts``.
    """
    in_thread = bool(thread_ts) and thread_ts != message_ts
    try:
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel_id,
            user=user_id,
            thread_ts=thread_ts if in_thread else None,
            text=text,
            blocks=blocks,
        )
    except CLICK_REPLY_ERRORS as err:
        log.info("slack.ephemeral_failed", error=error_name(err))


def error_name(err: BaseException) -> str:
    """The Slack error code, or the transport failure's type name, for a log line."""
    if isinstance(err, SlackApiError):
        return str(err.response.get("error", "slack_api_error"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
    return type(err).__name__
