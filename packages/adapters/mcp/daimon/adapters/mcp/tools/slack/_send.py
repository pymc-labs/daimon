"""Slack send_message implementation: post + composite thread targets.

Also owns thread-root creation (``_slack_create_thread_impl``): posting a
root message and returning its ``ts`` as the thread anchor.

Bot-token only. Unlike the read tools, send never uses the caller's own
xoxp token — there is no impersonation path and no hybrid client to fall
back to; every post is authenticated as the workspace bot.

Content is posted raw, with no entity escaping: send content is
deliberately authored (by the agent or a routine), so `<@U…>` mentions,
`<#C…>` channel links, and bold markers are meant to render, and escaping
them would break that intent.

A thread target is validated with `conversations.replies` before anything
is posted. Slack accepts a `thread_ts` that does not exist, silently drops
it, and posts the message at the channel root instead of erroring — so the
post itself can never surface a bad thread target; only a pre-flight check
can. The composite `channel_id:thread_ts` form is how a caller aims a reply
at a specific thread.
"""

from __future__ import annotations

from typing import Any, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.slack._client import (
    _require_slack_identity,  # pyright: ignore[reportPrivateUsage]
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    slack_web_client,
)
from daimon.adapters.mcp.tools.slack._models import SlackMessageRow
from daimon.adapters.mcp.tools.slack._visibility import (
    check_channel_access,
    map_slack_api_error,
)
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

# Slack's {"type": "markdown"} block cap is 12,000 CHARACTERS (not bytes);
# over it, chat.postMessage answers msg_too_long. Kept local to this module —
# it is platform-specific and must not leak into core.
_MAX_CONTENT_CHARS = 12_000

# Duplicated from the Slack adapter's bounded notification-text fallback
# (daimon.adapters.slack.lifecycle) — adapters must not import each other.
_NOTIFICATION_TEXT_MAX = 3000

_OVER_LENGTH_MSG = (
    f"content is over Slack's {_MAX_CONTENT_CHARS:,}-character message limit — "
    "shorten it, or send it in parts with several send_message calls "
    "(thread replies work well for parts)"
)
_NOT_IN_CHANNEL_MSG = "daimon isn't in that channel — ask a member to /invite @daimon"
_FILES_UNSUPPORTED_MSG = (
    "file posting is not available on Slack yet — this workspace's bot token "
    "has no files:write scope"
)
_THREAD_NOT_FOUND_MSG = "that thread does not exist — check the thread_ts and try again"


def _split_send_target(channel_id: str) -> tuple[str, str | None]:
    """Split a `channel_id` or `channel_id:thread_ts` composite target."""
    target, sep, thread_ts = channel_id.partition(":")
    if not sep:
        return channel_id, None
    if not target or not thread_ts:
        raise ToolError(
            "slack send targets have the form channel_id or channel_id:thread_ts "
            "(e.g. C0123456789:1717171717.123456)"
        )
    return target, thread_ts


def _notification_text(content: str) -> str:
    """Bound content for use as the `text` notification fallback."""
    if len(content) <= _NOTIFICATION_TEXT_MAX:
        return content
    return content[: _NOTIFICATION_TEXT_MAX - 1] + "…"


def _slack_error_code(err: SlackApiError) -> str:
    return str(err.response.get("error", ""))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like


async def _validate_channel_access(
    client: AsyncWebClient, *, channel_id: str, requester_id: str
) -> None:
    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        channel = cast(dict[str, Any], info["channel"])
        await check_channel_access(client, channel=channel, user_id=requester_id)
    except SlackApiError as err:
        mapped = map_slack_api_error(err)
        if mapped is None:
            raise
        raise mapped from err


async def _validate_thread_target(
    client: AsyncWebClient, *, channel_id: str, thread_ts: str
) -> None:
    try:
        await client.conversations_replies(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel_id, ts=thread_ts, limit=1
        )
    except SlackApiError as err:
        code = _slack_error_code(err)
        if code in ("thread_not_found", "message_not_found"):
            raise ToolError(_THREAD_NOT_FOUND_MSG) from err
        mapped = map_slack_api_error(err)
        if mapped is None:
            raise
        raise mapped from err


async def _post_message(
    client: AsyncWebClient, *, channel_id: str, content: str, thread_ts: str | None
) -> dict[str, Any]:
    post_kwargs: dict[str, Any] = {
        "channel": channel_id,
        "text": _notification_text(content),
        "blocks": [{"type": "markdown", "text": content}],
    }
    if thread_ts is not None:
        post_kwargs["thread_ts"] = thread_ts
    try:
        resp = await client.chat_postMessage(**post_kwargs)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]  # slack_sdk **kwargs: Unknown
    except SlackApiError as err:
        code = _slack_error_code(err)
        if code == "not_in_channel":
            raise ToolError(_NOT_IN_CHANNEL_MSG) from err
        if code == "msg_too_long":
            raise ToolError(_OVER_LENGTH_MSG) from err
        mapped = map_slack_api_error(err)
        if mapped is None:
            raise
        raise mapped from err
    return cast(dict[str, Any], resp.data)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # slack_sdk response is dict-like


async def _slack_send_message_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    content: str,
    attachments: list[dict[str, str]] | None,
    file_handles: list[str] | None,
) -> SlackMessageRow:
    if attachments or file_handles:
        raise ToolError(_FILES_UNSUPPORTED_MSG)
    if len(content) > _MAX_CONTENT_CHARS:
        raise ToolError(_OVER_LENGTH_MSG)

    target_channel_id, thread_ts = _split_send_target(channel_id)
    requester_id = _require_slack_identity(auth)
    team_id = _require_team_id(auth)
    client = await slack_web_client(runtime, team_id=team_id)

    await _validate_channel_access(client, channel_id=target_channel_id, requester_id=requester_id)
    if thread_ts is not None:
        await _validate_thread_target(client, channel_id=target_channel_id, thread_ts=thread_ts)

    resp = await _post_message(
        client, channel_id=target_channel_id, content=content, thread_ts=thread_ts
    )
    message = cast(dict[str, Any], resp.get("message") or {})
    response_thread_ts = message.get("thread_ts")
    response_user_id = message.get("user")
    return SlackMessageRow(
        ts=str(resp["ts"]),
        user_id=str(response_user_id) if response_user_id else None,
        text=content,
        thread_ts=str(response_thread_ts) if response_thread_ts else None,
    )


async def _slack_create_thread_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    content: str,
) -> SlackMessageRow:
    """Post a root message and return its ``ts`` as the thread anchor.

    No ``name`` parameter — Slack threads have no title. A composite
    ``channel_id:thread_ts`` target is refused: a thread root goes to a
    channel, not into an existing thread (use send_message for a reply).
    """
    if len(content) > _MAX_CONTENT_CHARS:
        raise ToolError(_OVER_LENGTH_MSG)

    target_channel_id, thread_ts = _split_send_target(channel_id)
    if thread_ts is not None:
        raise ToolError(
            "a thread root is posted to a channel, not into an existing thread — "
            "use send_message with the composite channel_id:thread_ts form to reply "
            "into an existing thread"
        )
    requester_id = _require_slack_identity(auth)
    team_id = _require_team_id(auth)
    client = await slack_web_client(runtime, team_id=team_id)

    await _validate_channel_access(client, channel_id=target_channel_id, requester_id=requester_id)

    resp = await _post_message(
        client, channel_id=target_channel_id, content=content, thread_ts=None
    )
    message = cast(dict[str, Any], resp.get("message") or {})
    response_thread_ts = message.get("thread_ts")
    response_user_id = message.get("user")
    return SlackMessageRow(
        ts=str(resp["ts"]),
        user_id=str(response_user_id) if response_user_id else None,
        text=content,
        thread_ts=str(response_thread_ts) if response_thread_ts else None,
    )
