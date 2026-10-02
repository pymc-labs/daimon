"""Shared rules for the channel tidy tools (edit_message, delete_message,
archive_thread, delete_thread) on Discord and Slack.

Ownership: a message is the calling agent's own only when
`agent_posted_messages` says this agent posted it. `send_message` and
`create_thread` write that row at send time (`record_agent_posts`), keyed by
the executing agent (`chat_agent_id` for a chat turn, `agent_id` for an
agent key). Anything else in a channel — a person's message, another
agent's post, another bot's, daimon's own turn replies, status cards,
credential cards and support-escalation posts — has no such row for this
agent and is refused. The platform side is checked too: on Discord the
message must be authored by daimon's bot user; Slack lets a bot token edit
and delete only its own messages.

Every action is decided at call time with a fresh policy: the caller's own
platform permission to post there, `require_channel_writable`
(`authorize(POST)`: protection, pins, isolation, channel admins), the seal
(only a turn inside a sealed channel may tidy it), and the configured
support-escalation channels, which are never tidied. Nothing these tools
return carries message text.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import (
    ChannelReadPolicy,
    get_verified_origin,
    load_read_policy,
    turn_origin_place,
)
from daimon.core.authz import Place
from daimon.core.channel_tidy import (
    PER_HOUR_LIMIT,
    PER_TURN_LIMIT,
    TidyActor,
    TidyLimitReached,
    TidyOperation,
    TidyTarget,
    content_hash,
    record_tidy_actions,
    record_tidy_outcome,
)
from daimon.core.security_audit import record_denial
from daimon.core.stores.agent_posts import (
    AgentPostRow,
    PostKind,
    get_post,
    mark_deleted,
    record_post,
    set_post_hash,
)
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

log = structlog.get_logger(__name__)

_NO_AGENT_MSG = "only an agent can tidy its own messages; this call runs as no agent"
_NO_TURN_MSG = (
    "pass this turn's origin_context_id: tidy limits are counted per turn. Do not retry without it."
)
_ESCALATION_MSG = (
    "this is the support-escalation channel: messages there are a shared record and "
    "daimon does not edit or delete them. Tell the caller. Do not retry."
)
_NOT_POSTED_MSG = (
    "this message was not posted by you through send_message or create_thread, so you "
    "cannot edit or delete it. You can only tidy your own posts. Do not retry."
)
_OTHER_AGENT_MSG = (
    "another agent posted this message, so you cannot edit or delete it. Do not retry."
)
_NOT_BOT_MSG = "this is not daimon's own post, so it cannot be changed. Do not retry."
_LIMIT_MSG = {
    "turn": (
        f"you have used this turn's tidy limit ({PER_TURN_LIMIT} edits or deletes). "
        "Stop and tell the caller what is left."
    ),
    "hour": (
        f"you have used this hour's tidy limit ({PER_HOUR_LIMIT} edits or deletes). "
        "Stop and tell the caller what is left."
    ),
}

RefusalReason = Literal["not_posted_by_agent", "other_agent", "not_bot_author", "not_own_thread"]


class TidyResult(BaseModel):
    """What changed. Ids only: no message text comes back."""

    platform: str
    channel_id: str
    message_id: str
    action: Literal["edited", "deleted", "archived"]
    messages_deleted: int = 0


@dataclass(frozen=True)
class TidyContext:
    """The resolved caller for one tidy call."""

    auth: AuthIdentity
    actor: TidyActor
    origin: Place | None
    read_policy: ChannelReadPolicy


def executing_agent_id(auth: AuthIdentity) -> uuid.UUID | None:
    """The agent a call runs as: the agent key's, else the chat turn's."""
    return auth.agent_id or auth.chat_agent_id


async def resolve_tidy_context(
    runtime: McpRuntime, auth: AuthIdentity, *, platform: str, origin_context_id: str | None
) -> TidyContext:
    """Who is acting, in which turn, and the read policy for the seal check.

    A chat turn names its turn with `origin_context_id`; an agent key's turn
    is its token. A call with neither is refused, since the per-turn limit
    has nothing to count against.
    """
    agent_id = executing_agent_id(auth)
    if agent_id is None:
        raise ToolError(_NO_AGENT_MSG)
    origin = await get_verified_origin(runtime, auth, origin_context_id or None)
    if origin is not None:
        turn_ref = f"origin:{origin.id}"
    elif auth.agent_id is not None and auth.token_jti is not None:
        turn_ref = f"token:{auth.token_jti}"
    else:
        raise ToolError(_NO_TURN_MSG)
    read_policy = await load_read_policy(runtime, auth, origin_context_id=origin_context_id or None)
    return TidyContext(
        auth=auth,
        actor=TidyActor(
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
            account_id=auth.account_id,
            platform=platform,
            platform_user_id=auth.platform_user_id,
            turn_ref=turn_ref,
        ),
        origin=turn_origin_place(origin) if origin is not None else None,
        read_policy=read_policy,
    )


def require_not_escalation_channel(
    runtime: McpRuntime, *, channel_id: str, parent_channel_id: str | None = None
) -> None:
    """Refuse the configured support-escalation channels and threads under them."""
    support = runtime.settings.support
    protected = {
        value
        for value in (support.escalation_channel_id, support.slack_escalation_channel_id)
        if isinstance(value, str) and value
    }
    if channel_id in protected or (
        parent_channel_id is not None and parent_channel_id in protected
    ):
        record_denial("escalation_channel")
        raise ToolError(_ESCALATION_MSG)


async def refuse(
    runtime: McpRuntime,
    ctx: TidyContext,
    *,
    tool_name: str,
    operation: TidyOperation,
    reason: RefusalReason,
    target: TidyTarget,
) -> ToolError:
    """Write a `denied` audit row for an ownership refusal and build its error."""
    async with runtime.session_factory.begin() as session:
        await record_tidy_outcome(
            session,
            actor=ctx.actor,
            tool_name=tool_name,
            operation=operation,
            outcome="denied",
            reason=reason,
            target=target,
            now=datetime.now(UTC),
        )
    record_denial(reason)
    message = {
        "not_posted_by_agent": _NOT_POSTED_MSG,
        "not_own_thread": _NOT_POSTED_MSG,
        "other_agent": _OTHER_AGENT_MSG,
        "not_bot_author": _NOT_BOT_MSG,
    }[reason]
    return ToolError(message)


async def require_own_post(
    runtime: McpRuntime,
    ctx: TidyContext,
    *,
    tool_name: str,
    operation: TidyOperation,
    channel_id: str,
    message_id: str,
    kind: PostKind = "message",
) -> AgentPostRow:
    """The ledger row proving this agent posted the target, or a refusal."""
    async with runtime.session_factory() as session:
        post = await get_post(
            session,
            tenant_id=ctx.actor.tenant_id,
            platform=ctx.actor.platform,
            channel_id=channel_id,
            message_id=message_id,
        )
    target = TidyTarget(channel_id=channel_id, message_id=message_id)
    if post is None or post.kind != kind:
        raise await refuse(
            runtime,
            ctx,
            tool_name=tool_name,
            operation=operation,
            reason="not_own_thread" if kind == "thread" else "not_posted_by_agent",
            target=target,
        )
    if post.agent_id != ctx.actor.agent_id:
        raise await refuse(
            runtime,
            ctx,
            tool_name=tool_name,
            operation=operation,
            reason="other_agent",
            target=target,
        )
    return post


async def begin_action(
    runtime: McpRuntime,
    ctx: TidyContext,
    *,
    tool_name: str,
    operation: TidyOperation,
    target: TidyTarget,
) -> None:
    """Check the limits and commit the `allowed` audit row before the platform call."""
    try:
        async with runtime.session_factory.begin() as session:
            await record_tidy_actions(
                session,
                actor=ctx.actor,
                tool_name=tool_name,
                operation=operation,
                targets=[target],
                now=datetime.now(UTC),
            )
    except TidyLimitReached as exc:
        record_denial("tidy_limit")
        raise ToolError(_LIMIT_MSG[exc.scope]) from exc


async def record_failure(
    runtime: McpRuntime,
    ctx: TidyContext,
    *,
    tool_name: str,
    operation: TidyOperation,
    target: TidyTarget,
    reason: str,
) -> None:
    """The platform refused after the `allowed` row was written."""
    async with runtime.session_factory.begin() as session:
        await record_tidy_outcome(
            session,
            actor=ctx.actor,
            tool_name=tool_name,
            operation=operation,
            outcome="error",
            reason=reason,
            target=target,
            now=datetime.now(UTC),
        )


async def finish_edit(runtime: McpRuntime, *, post: AgentPostRow, content: str) -> None:
    async with runtime.session_factory.begin() as session:
        await set_post_hash(session, post_id=post.id, content_sha256=content_hash(content))


async def finish_delete(runtime: McpRuntime, *, post_ids: list[uuid.UUID]) -> None:
    async with runtime.session_factory.begin() as session:
        await mark_deleted(session, post_ids=post_ids, now=datetime.now(UTC))


@dataclass(frozen=True)
class PostRecord:
    """One post to record at send time."""

    channel_id: str
    message_id: str
    kind: PostKind = "message"
    parent_channel_id: str | None = None
    thread_ts: str | None = None
    content: str | None = None


async def record_agent_posts(
    runtime: McpRuntime, auth: AuthIdentity, *, platform: str, posts: list[PostRecord]
) -> None:
    """Record what the executing agent just posted, so it can tidy it later.

    A call with no executing agent records nothing: no agent may tidy it.
    The post has already gone out, so a failure here is logged and swallowed;
    raising would make the agent retry and post twice. The cost is that the
    message cannot be tidied with these tools.
    """
    agent_id = executing_agent_id(auth)
    if agent_id is None or not posts:
        return
    try:
        async with runtime.session_factory.begin() as session:
            for post in posts:
                await record_post(
                    session,
                    tenant_id=auth.tenant_id,
                    platform=platform,
                    channel_id=post.channel_id,
                    message_id=post.message_id,
                    agent_id=agent_id,
                    kind=post.kind,
                    parent_channel_id=post.parent_channel_id,
                    thread_ts=post.thread_ts,
                    content_sha256=content_hash(post.content) if post.content is not None else None,
                )
    except Exception as exc:  # the post is out; never fail the send over its record
        log.warning(
            "channel_tidy.record_failed",
            tenant_id=str(auth.tenant_id),
            platform=platform,
            error_type=type(exc).__name__,
        )
