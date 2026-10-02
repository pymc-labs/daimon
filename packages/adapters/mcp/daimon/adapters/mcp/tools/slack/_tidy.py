"""Slack tidy tools: edit or delete the agent's own messages and threads.

Provides: _slack_edit_message_impl, _slack_delete_message_impl,
_slack_delete_thread_impl. The ownership, limit and audit rules live in
``tools/_tidy.py``. After the audit row is committed the write guard and the
seal are checked again on a fresh policy, right before the Slack call. Bot
token only: Slack itself lets a bot token change
only the bot's own messages, on top of the ledger check. Slack threads have
no archive state, so there is no archive_thread here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import require_channel_writable
from daimon.adapters.mcp.tools._tidy import (
    Check,
    TidyContext,
    TidyResult,
    finish_delete,
    finish_edit,
    policy_recheck,
    require_not_escalation_channel,
    require_own_post,
    resolve_tidy_context,
    run_action,
)
from daimon.adapters.mcp.tools.slack._client import (
    _require_slack_identity,  # pyright: ignore[reportPrivateUsage]
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    slack_web_client,
)
from daimon.adapters.mcp.tools.slack._send import (
    _MAX_CONTENT_CHARS,  # pyright: ignore[reportPrivateUsage]
    _notification_text,  # pyright: ignore[reportPrivateUsage]
    _slack_error_code,  # pyright: ignore[reportPrivateUsage]
    _validate_channel_access,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._visibility import map_slack_api_error
from daimon.core.channel_tidy import TidyOperation, TidyTarget
from daimon.core.stores.agent_posts import AgentPostRow, list_posts_in
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

_PLATFORM = "slack"
# A thread larger than this is not deleted by a tool call.
_MAX_THREAD_MESSAGES = 50


def _split_thread_id(thread_id: str) -> tuple[str, str]:
    channel_id, sep, thread_ts = thread_id.partition(":")
    if not sep or not channel_id or not thread_ts:
        raise ToolError(
            "slack thread ids have the form channel_id:thread_ts "
            "(e.g. C0123456789:1717171717.123456)"
        )
    return channel_id, thread_ts


async def _check_place(
    runtime: McpRuntime,
    ctx: TidyContext,
    auth: AuthIdentity,
    client: AsyncWebClient,
    *,
    channel_id: str,
) -> None:
    """The caller's access, the escalation guard and the tenant write guard."""
    await _validate_channel_access(
        client, channel_id=channel_id, requester_id=_require_slack_identity(auth)
    )
    require_not_escalation_channel(runtime, channel_id=channel_id)
    await require_channel_writable(runtime, auth, channel_id=channel_id, origin=ctx.origin)
    ctx.read_policy.require(channel_id)


def _require_thread_unsealed(ctx: TidyContext, channel_id: str, post: AgentPostRow) -> None:
    """A thread sealed on its own (``channel_id:thread_ts``) is tidied only from inside it."""
    ctx.read_policy.require(f"{channel_id}:{post.thread_ts or post.message_id}", channel_id)


def _recheck(
    runtime: McpRuntime,
    ctx: TidyContext,
    auth: AuthIdentity,
    *,
    channel_id: str,
    post: AgentPostRow,
) -> Check:
    async def writable() -> None:
        await require_channel_writable(runtime, auth, channel_id=channel_id, origin=ctx.origin)

    return policy_recheck(
        runtime,
        ctx,
        writable=writable,
        sealed=[
            (channel_id, None),
            (f"{channel_id}:{post.thread_ts or post.message_id}", channel_id),
        ],
    )


def _describe(verb: str) -> Callable[[Exception], ToolError | None]:
    def describe(exc: Exception) -> ToolError | None:
        return _platform_error(exc, verb) if isinstance(exc, SlackApiError) else None

    return describe


def _platform_error(err: SlackApiError, verb: str) -> ToolError:
    code = _slack_error_code(err)
    if code == "message_not_found":
        return ToolError("message not found")
    if code in ("cant_update_message", "cant_delete_message"):
        return ToolError(f"slack does not let daimon {verb} this message")
    if code == "edit_window_closed":
        return ToolError("this workspace no longer allows editing that message")
    if code == "msg_too_long":
        return ToolError(f"content is over Slack's {_MAX_CONTENT_CHARS:,}-character limit")
    mapped = map_slack_api_error(err)
    return mapped or ToolError(f"slack refused to {verb} the message: {code or 'unknown error'}")


async def _slack_edit_message_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    message_id: str,
    content: str,
    origin_context_id: str | None,
) -> TidyResult:
    if not content.strip():
        raise ToolError("content must not be empty; use delete_message to remove a message")
    if len(content) > _MAX_CONTENT_CHARS:
        raise ToolError(f"content is over Slack's {_MAX_CONTENT_CHARS:,}-character limit")
    ctx = await resolve_tidy_context(
        runtime, auth, platform=_PLATFORM, origin_context_id=origin_context_id
    )
    client = await slack_web_client(runtime, team_id=_require_team_id(auth))
    tool: str = "edit_message"
    operation: TidyOperation = "message.edit"
    await _check_place(runtime, ctx, auth, client, channel_id=channel_id)
    post = await require_own_post(
        runtime,
        ctx,
        tool_name=tool,
        operation=operation,
        channel_id=channel_id,
        message_id=message_id,
    )
    _require_thread_unsealed(ctx, channel_id, post)

    async def act() -> None:
        await client.chat_update(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel_id,
            ts=message_id,
            text=_notification_text(content),
            blocks=[{"type": "markdown", "text": content}],
        )

    await run_action(
        runtime,
        ctx,
        tool_name=tool,
        operation=operation,
        target=TidyTarget(
            channel_id=channel_id, message_id=message_id, content_hmac=post.content_hmac
        ),
        checks=[_recheck(runtime, ctx, auth, channel_id=channel_id, post=post)],
        act=act,
        describe_error=_describe("edit"),
    )
    await finish_edit(runtime, post=post, content=content)
    return TidyResult(
        platform=_PLATFORM, channel_id=channel_id, message_id=message_id, action="edited"
    )


async def _slack_delete_message_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    message_id: str,
    origin_context_id: str | None,
) -> TidyResult:
    ctx = await resolve_tidy_context(
        runtime, auth, platform=_PLATFORM, origin_context_id=origin_context_id
    )
    client = await slack_web_client(runtime, team_id=_require_team_id(auth))
    tool: str = "delete_message"
    operation: TidyOperation = "message.delete"
    await _check_place(runtime, ctx, auth, client, channel_id=channel_id)
    post = await require_own_post(
        runtime,
        ctx,
        tool_name=tool,
        operation=operation,
        channel_id=channel_id,
        message_id=message_id,
    )
    _require_thread_unsealed(ctx, channel_id, post)

    async def act() -> None:
        await client.chat_delete(channel=channel_id, ts=message_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown

    await run_action(
        runtime,
        ctx,
        tool_name=tool,
        operation=operation,
        target=TidyTarget(
            channel_id=channel_id, message_id=message_id, content_hmac=post.content_hmac
        ),
        checks=[_recheck(runtime, ctx, auth, channel_id=channel_id, post=post)],
        act=act,
        describe_error=_describe("delete"),
    )
    await finish_delete(runtime, post_ids=[post.id])
    return TidyResult(
        platform=_PLATFORM,
        channel_id=channel_id,
        message_id=message_id,
        action="deleted",
        messages_deleted=1,
    )


async def _thread_message_ts(
    client: AsyncWebClient, *, channel_id: str, thread_ts: str
) -> list[str]:
    """Every ts in the thread, root first, up to one past the cap."""
    try:
        resp = await client.conversations_replies(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel_id, ts=thread_ts, limit=_MAX_THREAD_MESSAGES + 1
        )
    except SlackApiError as err:
        if _slack_error_code(err) in ("thread_not_found", "message_not_found"):
            raise ToolError("thread not found") from err
        raise _platform_error(err, "read") from err
    raw = cast(list[dict[str, Any]], resp["messages"])
    metadata = cast(dict[str, Any], resp.get("response_metadata") or {})
    ts_list = [str(m["ts"]) for m in raw]
    if metadata.get("next_cursor") or len(ts_list) > _MAX_THREAD_MESSAGES:
        raise ToolError(
            f"this thread has more than {_MAX_THREAD_MESSAGES} messages, so it is not "
            "deleted in one call"
        )
    return ts_list


async def _slack_delete_thread_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    thread_id: str,
    origin_context_id: str | None,
) -> TidyResult:
    """Delete a thread whose root and every reply this agent posted.

    Only the agent's own messages are deleted, by ts: a reply someone posts
    after the read survives, and Slack keeps it under a "deleted" root.
    Replies go first, then the root, so a failure part-way leaves the root
    and says how many replies were removed. A thread anyone else replied in
    is refused: delete your own replies with delete_message instead.
    """
    channel_id, thread_ts = _split_thread_id(thread_id)
    ctx = await resolve_tidy_context(
        runtime, auth, platform=_PLATFORM, origin_context_id=origin_context_id
    )
    client = await slack_web_client(runtime, team_id=_require_team_id(auth))
    tool: str = "delete_thread"
    operation: TidyOperation = "thread.delete"
    await _check_place(runtime, ctx, auth, client, channel_id=channel_id)
    root = await require_own_post(
        runtime,
        ctx,
        tool_name=tool,
        operation=operation,
        channel_id=channel_id,
        message_id=thread_ts,
    )
    if root.thread_ts not in (None, thread_ts):
        raise ToolError("that ts is a reply, not a thread root; use delete_message for it")
    _require_thread_unsealed(ctx, channel_id, root)
    ts_list = await _thread_message_ts(client, channel_id=channel_id, thread_ts=thread_ts)
    async with runtime.session_factory() as session:
        own = await list_posts_in(
            session,
            tenant_id=ctx.actor.tenant_id,
            platform=_PLATFORM,
            channel_id=channel_id,
            message_ids=ts_list,
        )
    own_by_ts = {p.message_id: p for p in own if p.agent_id == ctx.actor.agent_id}
    if any(ts not in own_by_ts for ts in ts_list):
        raise ToolError(
            "this thread has replies that are not yours, so it cannot be deleted. "
            "Use delete_message on your own replies instead."
        )
    replies = [ts for ts in ts_list if ts != thread_ts]
    deleted: list[str] = []

    async def act() -> None:
        for ts in [*replies, thread_ts]:
            await client.chat_delete(channel=channel_id, ts=ts)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            deleted.append(ts)

    def describe(exc: Exception) -> ToolError | None:
        if not isinstance(exc, SlackApiError):
            return None
        return ToolError(
            f"slack refused part-way: {len(deleted)} of {len(ts_list)} messages were deleted "
            f"({_slack_error_code(exc) or 'unknown error'})"
        )

    try:
        await run_action(
            runtime,
            ctx,
            tool_name=tool,
            operation=operation,
            target=TidyTarget(channel_id=channel_id, message_id=thread_ts),
            checks=[_recheck(runtime, ctx, auth, channel_id=channel_id, post=root)],
            act=act,
            describe_error=describe,
        )
    finally:
        await finish_delete(runtime, post_ids=[own_by_ts[ts].id for ts in deleted])
    return TidyResult(
        platform=_PLATFORM,
        channel_id=channel_id,
        message_id=thread_ts,
        action="deleted",
        messages_deleted=len(deleted),
    )
