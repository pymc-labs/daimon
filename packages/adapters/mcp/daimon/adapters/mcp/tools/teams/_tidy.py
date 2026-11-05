"""Teams edit_message, delete_message and delete_reply: an agent changes only what it posted.

Ownership, limits and audit rules are the shared ones in tools/_tidy.py.
Bot Framework also lets the bot update and delete only its own activities.
A post is keyed by its conversation (what ``send_message`` or
``create_thread`` returned, or the chat or thread a turn or routine posted
in) and its activity id. A thread's root may be recorded under its channel
or under the thread it roots, so a lookup that misses tries the other form,
and the call then acts on the form it found. A turn's or routine's post also takes the
conversation rules: the turn is over, and the caller asked for it or is a
Teams admin (an owner of the admin team).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._tidy import (
    Check,
    MessageGone,
    TidyContext,
    TidyResult,
    delete_parts,
    policy_recheck,
    reply_parts,
    require_conversation_rights,
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
from daimon.core.stores.agent_posts import AgentPostRow, get_post
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


async def _ledger_key(
    runtime: McpRuntime, ctx: TidyContext, conversation_id: str, message_id: str
) -> str:
    """The form of `conversation_id` the post is recorded under, for a thread's root."""
    channel, root = split_thread(conversation_id)
    if conversation_id.startswith("a:") or root not in (None, message_id):
        return conversation_id
    forms = (conversation_id, channel if root else thread_id(channel, message_id))
    async with runtime.session_factory() as session:
        for form in forms:
            found = await get_post(
                session,
                tenant_id=ctx.actor.tenant_id,
                platform=_PLATFORM,
                channel_id=form,
                message_id=message_id,
            )
            if found is not None:
                return form
    return conversation_id


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
                return MessageGone("message not found")
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
    named = _conversation_id(channel_id)
    ctx = await resolve_tidy_context(
        runtime, auth, platform=_PLATFORM, origin_context_id=origin_context_id
    )
    conversation_id = await _ledger_key(runtime, ctx, named, message_id)
    client = await _check_place(runtime, ctx, auth, conversation_id)
    post = await _check_post(
        runtime,
        ctx,
        auth,
        tool=tool,
        operation=operation,
        conversation_id=conversation_id,
        message_id=message_id,
    )
    return ctx, client, conversation_id, post


async def _check_post(
    runtime: McpRuntime,
    ctx: TidyContext,
    auth: AuthIdentity,
    *,
    tool: str,
    operation: TidyOperation,
    conversation_id: str,
    message_id: str,
) -> AgentPostRow:
    """Ownership, the conversation rules and the seal, in a place already checked."""
    post = await require_own_post(
        runtime,
        ctx,
        tool_name=tool,
        operation=operation,
        channel_id=conversation_id,
        message_id=message_id,
    )
    await require_conversation_rights(
        runtime,
        ctx,
        post,
        tool_name=tool,
        operation=operation,
        requester_is_admin=auth.admin_for(),
    )
    for channel, parent in _sealed(conversation_id, message_id):
        ctx.read_policy.require(channel, parent)
    return post


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
    await _delete_one(runtime, ctx, client, post, tool=tool)
    return TidyResult(
        platform=_PLATFORM,
        channel_id=conversation_id,
        message_id=message_id,
        action="deleted",
        messages_deleted=1,
    )


async def _delete_one(
    runtime: McpRuntime,
    ctx: TidyContext,
    client: TeamsBotClient,
    post: AgentPostRow,
    *,
    tool: str,
) -> None:
    """Delete one recorded message after its place, ownership and rights are checked."""
    conversation_id, message_id = post.channel_id, post.message_id

    async def act() -> None:
        await client.delete_activity(conversation_id, message_id)

    await run_action(
        runtime,
        ctx,
        tool_name=tool,
        operation="message.delete",
        target=TidyTarget(conversation_id, message_id, post.content_hmac),
        checks=[_recheck(conversation_id, post)],
        act=act,
        describe_error=_describe("delete"),
        post=post,
    )


async def _teams_delete_reply_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    message_id: str,
    origin_context_id: str | None,
) -> TidyResult:
    tool: str = "delete_reply"
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
    # A routine's chunks in a channel each root their own thread, so every
    # part's place is checked, and all before the first deletion.
    clients = {conversation_id: client}
    parts = await reply_parts(runtime, post)
    for part in parts:
        if part.channel_id not in clients:
            clients[part.channel_id] = await _check_place(runtime, ctx, auth, part.channel_id)
        await _check_post(
            runtime,
            ctx,
            auth,
            tool=tool,
            operation=operation,
            conversation_id=part.channel_id,
            message_id=part.message_id,
        )

    async def delete(part: AgentPostRow) -> None:
        await _delete_one(runtime, ctx, clients[part.channel_id], part, tool=tool)

    deleted = await delete_parts(runtime, parts, delete)
    return TidyResult(
        platform=_PLATFORM,
        channel_id=conversation_id,
        message_id=message_id,
        action="deleted",
        messages_deleted=deleted,
    )
