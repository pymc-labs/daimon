"""The tenant access policy as the channel tools see it.

One write guard for Discord, Slack and Teams: each platform resolves its target to a
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
from dataclasses import dataclass, field
from datetime import UTC, datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity, token_channel_id
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_place, mcp_subject
from daimon.core.access_policy import (
    OPEN_ACCESS_POLICY,
    TenantAccessPolicy,
    isolated_channel_of,
)
from daimon.core.authz import Action, AgentRef, Place, Subject, authorize, build_agent_ref
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.security_audit import record_authz_denial
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.turn_origins import get_active_origin
from fastmcp.exceptions import ToolError

_PROTECTED_MSG = (
    "this channel is protected: the workspace does not let daimon post there. "
    "Tell the caller and offer to post somewhere else. Do not retry."
)
_ISOLATED_WRITE_MSG = (
    "this channel is isolated: only its own agents post in it. Tell the caller. Do not retry."
)
_HELD_SEND_MSG = (
    "this conversation is in an isolated channel, so nothing said here is posted or sent "
    "outside it. Tell the caller. Do not retry."
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
    origin: Place | None = None,
) -> None:
    """Raise ToolError when the tenant policy forbids this post (`authorize(POST)`).

    Every channel send path (messages, replies, thread and post creation, file
    and card posts) calls this. Besides channel protection it holds a pinned
    agent to its pin: wherever the turn was admitted -- including an admin's DM
    or hub turn, which a pin exempts -- the agent may post only into its pinned
    channels and threads under them, so its context never reaches another
    channel. The requester's own 1:1 DM with daimon is allowed: only they see it.

    An isolated channel takes posts only from its own agents, and they post
    nowhere else, not even into the requester's DM. ``origin`` is the calling
    turn's verified place (`turn_origin_place`): a call from inside an isolated
    channel posts only there, and one from its setup thread may post into it.
    """
    policy = await load_channel_policy(runtime, auth)
    place = Place(
        channel_id=channel_id,
        parent_channel_id=parent_channel_id,
        category_id=category_id,
        own_dm=_is_requesters_own_dm(channel_id),
    )
    subject = mcp_subject(auth)
    # Protection, the own-DM allowance and an unpinned tenant need no agent
    # lookup; settle those first.
    first = authorize(policy, subject=subject, action=Action.POST, place=place)
    if first.reason == "channel_protected":
        record_authz_denial(Action.POST, first.reason)
        raise ToolError(_PROTECTED_MSG)
    if not policy.isolated_channel_ids and (place.own_dm or not policy.agent_channel_pins):
        return
    agent = await _executing_agent(runtime, auth, policy)
    decision = authorize(
        policy, subject=subject, action=Action.POST, agent=agent, place=place, origin=origin
    )
    if not decision:
        record_authz_denial(Action.POST, decision.reason)
    if decision.reason == "channel_isolated":
        if isolated_channel_of(policy, channel_id, parent_channel_id):
            raise ToolError(_ISOLATED_WRITE_MSG)
        if _origin_isolated(policy, origin):
            raise ToolError(_HELD_SEND_MSG)
    if not decision:
        raise ToolError(_PINNED_SEND_MSG)


def _origin_isolated(policy: TenantAccessPolicy, origin: Place | None) -> bool:
    return origin is not None and (
        isolated_channel_of(policy, origin.channel_id, origin.parent_channel_id) is not None
    )


def turn_origin_place(origin: TurnOriginRow) -> Place:
    """Where a verified turn origin runs, marking a setup conversation's thread."""
    return Place.from_origin(
        parent_channel_id=origin.parent_channel_id,
        thread_id=origin.thread_id,
        setup_thread=origin.is_setup,
    )


def _is_requesters_own_dm(channel_id: str) -> bool:
    """A 1:1 conversation with the bot: a Slack ``D…`` IM or a Teams ``a:…`` chat.

    Every send path checks the requester is in the target before the write
    guard runs, so such a conversation is the requester's own DM with daimon
    -- the place an admin's exempt turn answers.
    """
    return channel_id.startswith("D") or channel_id.startswith("a:")


async def _executing_agent(
    runtime: McpRuntime, auth: AuthIdentity, policy: TenantAccessPolicy
) -> AgentRef:
    """The agent this call executes as, for the pin rules.

    The executing agent is the turn's (``chat_agent_id``) or the agent key's
    (``agent_id``); an identity with neither is the operator, which no pin
    binds. An agent that can't be resolved is `AgentRef.unresolved`, which
    fails closed while pins exist.
    """
    executing = auth.chat_agent_id or auth.agent_id
    if executing is None or not (policy.agent_channel_pins or policy.isolated_channel_ids):
        return AgentRef.none()
    agent = await find_agent_by_derived_uuid(
        runtime.client, tenant_id=auth.tenant_id, agent_id=executing
    )
    if agent is None:
        return AgentRef.unresolved()
    return build_agent_ref(agent.name, agent.metadata)


async def require_dm_recipient_allowed(
    runtime: McpRuntime, auth: AuthIdentity, *, recipient_id: str
) -> None:
    """A pinned agent may DM only the person it is answering (`authorize(DIRECT_MESSAGE)`).

    Keeps a pinned agent admitted in an admin's DM or hub turn from carrying
    its context to any other workspace member through a direct message.
    """
    if auth.chat_agent_id is None and auth.agent_id is None:
        return
    policy = await load_channel_policy(runtime, auth)
    decision = authorize(
        policy,
        subject=mcp_subject(auth),
        action=Action.DIRECT_MESSAGE,
        agent=await _executing_agent(runtime, auth, policy),
        recipient_id=recipient_id,
    )
    if not decision:
        record_authz_denial(Action.DIRECT_MESSAGE, decision.reason)
    if decision.reason == "agent_unresolved":
        raise ToolError(_PINNED_SEND_MSG)
    if decision.reason == "channel_isolated":
        raise ToolError(
            "this agent belongs to an isolated channel, so it sends no direct messages: "
            "what it knows stays in that channel. Tell the caller. Do not retry."
        )
    if not decision:
        raise ToolError(
            "this agent is pinned to its own channels, so it can only send a direct "
            "message to the person it is answering. Tell the caller. Do not retry."
        )


async def require_agent_creatable(
    runtime: McpRuntime, auth: AuthIdentity, *, origin: Place | None = None
) -> None:
    """An isolated channel's own agent, a key bound inside one, or a call whose
    verified turn origin (`turn_origin_place`) lies in one, such as its setup
    thread, creates no agent (`authorize(CREATE_AGENT)`): a new agent answers
    outside it, so a prompt written there would carry the channel's content out.
    A chat turn that names no verified origin is refused too: it may be running
    in that setup thread, and judged from outside it would slip the rule."""
    if auth.chat_agent_id is None and auth.agent_id is None:
        return
    policy = await load_channel_policy(runtime, auth)
    if not policy.isolated_channel_ids:
        return
    decision = authorize(
        policy,
        subject=mcp_subject(auth),
        action=Action.CREATE_AGENT,
        agent=await _executing_agent(runtime, auth, policy),
        origin=origin or (mcp_place(auth) if token_channel_id(auth) is not None else None),
    )
    if not decision:
        record_authz_denial(Action.CREATE_AGENT, decision.reason)
        raise ToolError(
            "this conversation is held to an isolated channel, so it creates no agents: a "
            "new agent would answer outside it. Tell the caller to create it from a "
            "conversation outside that channel. Nothing was created. Do not retry."
        )
    if auth.agent_id is None and origin is None:
        record_authz_denial(Action.CREATE_AGENT, "origin_missing")
        raise ToolError(
            "create_agent needs a verified origin_context_id from a chat turn while a "
            "channel in this workspace is isolated, and this call has none. Nothing was "
            "created. Tell the caller to create the agent from a chat conversation. "
            "Do not retry."
        )


class SealedChannelError(ToolError):
    """A read of a sealed channel from outside it. Not an access problem the
    caller can fix by connecting an account, so no connect hint follows it."""


@dataclass(frozen=True)
class ChannelReadPolicy:
    """The tenant policy plus the channels the calling turn runs in.

    `agent` is the executing agent while the tenant isolates a channel: only
    that channel's own agents read it. `origin` is the verified turn origin
    the ids came from, if any.
    """

    policy: TenantAccessPolicy
    origin_channel_ids: frozenset[str] = frozenset()
    agent: AgentRef = field(default_factory=AgentRef.none)
    origin: TurnOriginRow | None = None

    def allows(self, channel_id: str, parent_channel_id: str | None = None) -> bool:
        return bool(
            authorize(
                self.policy,
                subject=Subject(),
                action=Action.READ_CHANNEL,
                agent=self.agent,
                place=Place(channel_id=channel_id, parent_channel_id=parent_channel_id),
                origin_channel_ids=self.origin_channel_ids,
            )
        )

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
    An agent key minted in a channel needs no origin: its calls run inside
    that channel (`token_channel_id`), and only that channel. While a channel
    is isolated the executing agent is resolved too, for the isolation rule.
    """
    policy = await load_channel_policy(runtime, auth)
    agent = (
        await _executing_agent(runtime, auth, policy)
        if policy.isolated_channel_ids
        else AgentRef.none()
    )
    bound = token_channel_id(auth)
    if bound is not None:
        return ChannelReadPolicy(policy, frozenset({bound}), agent)
    if not (policy.sealed_channel_ids or resolve_without_seals):
        return ChannelReadPolicy(policy, agent=agent)
    origin = await get_verified_origin(runtime, auth, origin_context_id)
    if origin is None:
        return ChannelReadPolicy(policy, agent=agent)
    inside = {origin.parent_channel_id, origin.thread_id}
    if auth.platform == "slack":
        # A Slack thread is sealed by its channel_id:thread_ts form.
        inside.add(f"{origin.parent_channel_id}:{origin.thread_id}")
    return ChannelReadPolicy(policy, frozenset(inside), agent, origin)


async def get_verified_origin(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None
) -> TurnOriginRow | None:
    """The caller's active turn origin, verified as `load_read_policy` describes; None
    when none was named or it is malformed, expired, another account's or another
    responder's."""
    executing_agent = auth.agent_id or auth.chat_agent_id
    if not origin_context_id or auth.platform is None or executing_agent is None:
        return None
    try:
        origin_id = uuid.UUID(origin_context_id)
    except ValueError:
        return None
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
        return None
    return origin
