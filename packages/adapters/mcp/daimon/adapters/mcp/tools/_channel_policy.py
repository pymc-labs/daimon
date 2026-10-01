"""The tenant access policy as the channel tools see it.

One write guard for Discord and Slack: each platform resolves its target to a
channel id (plus parent channel and category where it has them) and calls
`require_channel_writable` after its own caller-permission check, so the
policy never reveals a channel the caller could not see anyway. Protection
and isolation apply to admins too.

Reads go through `ChannelReadPolicy`, which the channel dispatcher
(`tools/channels.py`) loads once per call and hands to the platform impl. A
sealed channel -- or a thread under one -- is readable only when the call
names the origin of a turn inside that same channel; with no origin, or a
foreign one, it is refused and its search hits are withheld.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._isolation import load_caller_isolation
from daimon.core.access_policy import (
    OPEN_ACCESS_POLICY,
    TenantAccessPolicy,
    is_outside_agent_pin,
    is_write_protected,
    isolated_channel_of,
)
from daimon.core.agent_pins import agent_pin_names
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.turn_origins import get_active_origin
from fastmcp.exceptions import ToolError

_PROTECTED_MSG = (
    "this channel is protected: the workspace does not let daimon post there. "
    "Tell the caller and offer to post somewhere else. Do not retry."
)
_ISOLATED_WRITE_MSG = (
    "this channel is isolated: only its own agents post in it. Tell the caller. Do not retry."
)
_PINNED_SEND_MSG = (
    "this agent is pinned to its own channels, so it can only post in them (and "
    "threads under them). Tell the caller and offer to post in one of its channels. "
    "Do not retry."
)
_UNREADABLE_MSG = (
    "this workspace's access policy could not be read, so daimon won't read or post channels"
)
_SEALED_MSG = (
    "this channel is sealed: it can only be read from a conversation inside it. "
    "If you are answering in that channel, pass this turn's origin_context_id; "
    "otherwise tell the caller. Do not retry."
)


async def load_channel_policy(runtime: McpRuntime, auth: AuthIdentity) -> TenantAccessPolicy:
    """Load the caller's tenant policy; an unreadable one refuses the call."""
    try:
        async with runtime.session_factory() as session:
            return await load_access_policy(session, tenant_id=auth.tenant_id)
    except AccessPolicyUnreadable as exc:
        raise ToolError(_UNREADABLE_MSG) from exc


async def require_channel_writable(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    parent_channel_id: str | None = None,
    category_id: str | None = None,
) -> None:
    """Raise ToolError when the tenant policy protects the target from agent writes.

    Every channel send path (messages, replies, thread and post creation, file
    and card posts) calls this. Besides channel protection it holds a pinned
    agent to its pin: wherever the turn was admitted -- including an admin's DM
    or hub turn, which a pin exempts -- the agent may post only into its pinned
    channels and threads under them, so its context never reaches another
    channel.

    An isolated channel takes posts only from its own agents; their pin keeps
    them from posting anywhere else.
    """
    policy = await load_channel_policy(runtime, auth)
    if is_write_protected(
        policy,
        channel_id=channel_id,
        parent_channel_id=parent_channel_id,
        category_id=category_id,
    ):
        raise ToolError(_PROTECTED_MSG)
    target = isolated_channel_of(policy, channel_id, parent_channel_id)
    if target is not None:
        caller = await load_caller_isolation(runtime, auth)
        if caller.inside_channel_id != target:
            raise ToolError(_ISOLATED_WRITE_MSG)
    if policy.agent_channel_pins:
        await _require_send_inside_pin(
            runtime, auth, policy, channel_id=channel_id, parent_channel_id=parent_channel_id
        )


def _is_requesters_own_dm(channel_id: str) -> bool:
    """A 1:1 conversation with the bot: a Slack ``D…`` IM or a Teams ``a:…`` chat.

    Every send path checks the requester is in the target before the write
    guard runs, so such a conversation is the requester's own DM with daimon
    -- the place an admin's exempt turn answers.
    """
    return channel_id.startswith("D") or channel_id.startswith("a:")


async def _executing_agent_names(
    runtime: McpRuntime, auth: AuthIdentity, policy: TenantAccessPolicy
) -> tuple[str | None, ...] | None:
    """Every name the executing agent answers to, or None when no pin binds it.

    The executing agent is the turn's (``chat_agent_id``) or the agent key's
    (``agent_id``); an identity with neither is the operator, which no pin
    binds. An agent that can't be resolved while pins exist fails closed.
    """
    executing = auth.chat_agent_id or auth.agent_id
    if executing is None or not policy.agent_channel_pins:
        return None
    agent = await find_agent_by_derived_uuid(
        runtime.client, tenant_id=auth.tenant_id, agent_id=executing
    )
    if agent is None:
        raise ToolError(_PINNED_SEND_MSG)
    names = agent_pin_names(agent.name, agent.metadata)
    if not any(name is not None and name in policy.agent_channel_pins for name in names):
        return None
    return names


async def _require_send_inside_pin(
    runtime: McpRuntime,
    auth: AuthIdentity,
    policy: TenantAccessPolicy,
    *,
    channel_id: str,
    parent_channel_id: str | None,
) -> None:
    """Refuse a pinned executing agent's post outside its pinned channels.

    Same rule as admission (`is_outside_agent_pin`): outside the pin of ANY
    name the agent answers to is refused. The requester's own 1:1 DM with
    daimon is allowed: only they see it.
    """
    if _is_requesters_own_dm(channel_id):
        return
    names = await _executing_agent_names(runtime, auth, policy)
    if names is None:
        return
    if is_outside_agent_pin(
        policy,
        agent_names=names,
        channel_id=channel_id,
        parent_channel_id=parent_channel_id,
    ):
        raise ToolError(_PINNED_SEND_MSG)


async def require_dm_recipient_allowed(
    runtime: McpRuntime, auth: AuthIdentity, *, recipient_id: str
) -> None:
    """A pinned agent may DM only the person it is answering.

    Keeps a pinned agent admitted in an admin's DM or hub turn from carrying
    its context to any other workspace member through a direct message.
    """
    if auth.chat_agent_id is None and auth.agent_id is None:
        return
    policy = await load_channel_policy(runtime, auth)
    if await _executing_agent_names(runtime, auth, policy) is None:
        return
    if recipient_id != auth.platform_user_id:
        raise ToolError(
            "this agent is pinned to its own channels, so it can only send a direct "
            "message to the person it is answering. Tell the caller. Do not retry."
        )


class SealedChannelError(ToolError):
    """A read of a sealed channel from outside it. Not an access problem the
    caller can fix by connecting an account, so no connect hint follows it."""


@dataclass(frozen=True)
class ChannelReadPolicy:
    """The tenant policy plus the channels the calling turn runs in."""

    policy: TenantAccessPolicy
    origin_channel_ids: frozenset[str] = frozenset()

    def allows(self, channel_id: str, parent_channel_id: str | None = None) -> bool:
        sealed = self.policy.sealed_channel_ids
        if channel_id in sealed:
            return channel_id in self.origin_channel_ids
        if parent_channel_id is not None and parent_channel_id in sealed:
            return parent_channel_id in self.origin_channel_ids
        return True

    def require(self, channel_id: str, parent_channel_id: str | None = None) -> None:
        """Raise ToolError for a sealed target outside the calling turn.

        Call after the platform's caller-permission check."""
        if not self.allows(channel_id, parent_channel_id):
            raise SealedChannelError(_SEALED_MSG)


# What an impl reads with when no dispatcher loaded a policy (direct test calls).
OPEN_READ_POLICY = ChannelReadPolicy(policy=OPEN_ACCESS_POLICY)


async def load_read_policy(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str | None,
    resolve_without_seals: bool = False,
) -> ChannelReadPolicy:
    """Load the tenant policy and, if one was named, the caller's active turn origin.

    With nothing sealed the origin is skipped, since no channel read needs it;
    ``resolve_without_seals`` resolves it anyway, for a check against a seal a
    session recorded rather than the current policy.

    The origin must belong to the caller's account and to the agent the token
    executes as: ``agent_id`` for agent-session tokens, ``chat_agent_id`` for
    ordinary chat. A token bound to neither can't claim an origin at all, so a
    sealed read with it is judged from outside. An origin that is malformed,
    expired, another account's or another responder's counts as none too.
    """
    policy = await load_channel_policy(runtime, auth)
    outside = ChannelReadPolicy(policy=policy)
    executing_agent = auth.agent_id or auth.chat_agent_id
    if (
        not (policy.sealed_channel_ids or resolve_without_seals)
        or not origin_context_id
        or auth.platform is None
        or executing_agent is None
    ):
        return outside
    try:
        origin_id = uuid.UUID(origin_context_id)
    except ValueError:
        return outside
    async with runtime.session_factory() as session:
        origin = await get_active_origin(
            session,
            origin_id=origin_id,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            platform=auth.platform,
            now=datetime.now(UTC),
        )
    if origin is None or executing_agent != derive_agent_uuid(
        tenant_id=auth.tenant_id, ma_agent_id=origin.responder_ma_agent_id
    ):
        return outside
    inside = {origin.parent_channel_id, origin.thread_id}
    if auth.platform == "slack":
        # A Slack thread is sealed by its channel_id:thread_ts form.
        inside.add(f"{origin.parent_channel_id}:{origin.thread_id}")
    return ChannelReadPolicy(policy=policy, origin_channel_ids=frozenset(inside))
