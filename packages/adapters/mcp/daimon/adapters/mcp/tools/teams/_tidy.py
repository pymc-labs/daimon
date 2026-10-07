"""Teams edit_message and delete_message: an agent changes only what it posted.

Ownership, limits and audit rules are the shared ones in tools/_tidy.py.
Bot Framework also lets the bot update and delete only its own activities.
A post is keyed by the conversation ``send_message`` or ``create_thread``
returned and its activity id.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._tidy import (
    Check,
    TidyContext,
    TidyResult,
    policy_recheck,
    require_not_escalation_channel,
    require_own_post,
    resolve_tidy_context,
    run_action,
)
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.mcp.tools.teams._directory import split_thread, thread_id
from daimon.adapters.mcp.tools.teams._send import (
    _authorize,  # pyright: ignore[reportPrivateUsage]
    _check_text,  # pyright: ignore[reportPrivateUsage]
    _conversation_id,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.channel_tidy import TidyOperation, TidyTarget
from daimon.core.stores.agent_posts import AgentPostRow
from fastmcp.exceptions import ToolError

_PLATFORM = "teams"


def _sealed(conversation_id: str, activity_id: str) -> list[tuple[str, str | None]]:
    """The channel, then the thread the message is in; a channel post roots its own."""
    channel, root = split_thread(conversation_id)
    if conversation_id.startswith("a:"):  # a 1:1 chat has no threads
        return [(conversation_id, None)]
    return [(channel, None), (thread_id(channel, root or activity_id), channel)]


async def _check_place(
    runtime: McpRuntime, ctx: TidyContext, auth: AuthIdentity, conversation_id: str
) -> TeamsBotClient:
    """The caller's roster and the tenant write guard, the escalation guard and the seal."""
    client = await _authorize(runtime, auth, conversation_id, ctx.origin)
    channel, _ = split_thread(conversation_id)
    require_not_escalation_channel(runtime, channel_id=conversation_id, parent_channel_id=channel)
    return client


def _recheck(conversation_id: str, post: AgentPostRow) -> Check:
    channel, _ = split_thread(conversation_id)
    return policy_recheck(
        channel_id=conversation_id,
        parent_channel_id=channel,
        sealed=_sealed(conversation_id, post.message_id),
    )


def _describe(verb: str) -> Callable[[Exception], ToolError | None]:
    def describe(exc: Exception) -> ToolError | None:
        if isinstance(exc, httpx.HTTPStatusError):
            if exc.response.status_code == 404:
                return ToolError("message not found")
            if exc.response.status_code == 403:
                return ToolError(f"teams does not let daimon {verb} this message")
            return ToolError(f"teams refused to {verb} the message ({exc.response.status_code})")
        if isinstance(exc, ValueError):
            return ToolError("that is not a Teams message id")
        return None

    return describe


async def _prepare(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    tool: str,
    operation: TidyOperation,
    channel_id: str,
    message_id: str,
    origin_context_id: str | None,
) -> tuple[TidyContext, TeamsBotClient, str, AgentPostRow]:
    conversation_id = _conversation_id(channel_id)
    ctx = await resolve_tidy_context(
        runtime, auth, platform=_PLATFORM, origin_context_id=origin_context_id
    )
    client = await _check_place(runtime, ctx, auth, conversation_id)
    post = await require_own_post(
        runtime,
        ctx,
        tool_name=tool,
        operation=operation,
        channel_id=conversation_id,
        message_id=message_id,
    )
    for channel, parent in _sealed(conversation_id, message_id):
        ctx.read_policy.require(channel, parent)
    return ctx, client, conversation_id, post


async def _teams_edit_message_impl(  # pyright: ignore[reportUnusedFunction]
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
    _check_text(content)
    tool: str = "edit_message"
    operation: TidyOperation = "message.edit"
    ctx, client, conversation_id, post = await _prepare(
        runtime,
        auth,
        tool=tool,
        operation=operation,
        channel_id=channel_id,
        message_id=message_id,
        origin_context_id=origin_context_id,
    )

    async def act() -> None:
        await client.update_text(conversation_id, message_id, content)

    await run_action(
        runtime,
        ctx,
        tool_name=tool,
        operation=operation,
        target=TidyTarget(conversation_id, message_id, post.content_hmac),
        checks=[_recheck(conversation_id, post)],
        act=act,
        describe_error=_describe("edit"),
        post=post,
        content=content,
    )
    return TidyResult(
        platform=_PLATFORM, channel_id=conversation_id, message_id=message_id, action="edited"
    )


async def _teams_delete_message_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    message_id: str,
    origin_context_id: str | None,
) -> TidyResult:
    tool: str = "delete_message"
    operation: TidyOperation = "message.delete"
    ctx, client, conversation_id, post = await _prepare(
        runtime,
        auth,
        tool=tool,
        operation=operation,
        channel_id=channel_id,
        message_id=message_id,
        origin_context_id=origin_context_id,
    )

    async def act() -> None:
        await client.delete_activity(conversation_id, message_id)

    await run_action(
        runtime,
        ctx,
        tool_name=tool,
        operation=operation,
        target=TidyTarget(conversation_id, message_id, post.content_hmac),
        checks=[_recheck(conversation_id, post)],
        act=act,
        describe_error=_describe("delete"),
        post=post,
    )
    return TidyResult(
        platform=_PLATFORM,
        channel_id=conversation_id,
        message_id=message_id,
        action="deleted",
        messages_deleted=1,
    )
