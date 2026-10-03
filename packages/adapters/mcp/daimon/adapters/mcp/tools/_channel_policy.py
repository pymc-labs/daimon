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
foreign one, it is refused and its search hits are withheld. A call held to
an isolated channel (its own agent, or an origin inside it) reads and lists
nothing outside it. A chat turn's token names no turn, so while one of its
turns runs inside an isolated channel (`_held_origin`) every call is held there,
whatever origin it names.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity, token_channel_id
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_place, mcp_subject
from daimon.core.access_policy import (
    OPEN_ACCESS_POLICY,
    TenantAccessPolicy,
)
from daimon.core.authz import (
    Action,
    AgentRef,
    Decision,
    Place,
    Subject,
    authorize,
    build_agent_ref,
    isolation_hold,
)
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.permissions import (
    any_confidential,
    any_pinned,
    any_sealed,
    confidential_channel_of,
)
from daimon.core.security_audit import record_authz_denial
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.turn_origins import get_active_origin, list_active_origins
from fastmcp.exceptions import ToolError

_PROTECTED_MSG = (
    "this channel is protected: the workspace does not let daimon post there. "
    "Tell the caller and offer to post somewhere else. Do not retry."
)
_ISOLATED_WRITE_MSG = (
    "this channel is confidential: only its own agents post in it. Tell the caller. Do not retry."
)
_HELD_SEND_MSG = (
    "this conversation is in a confidential channel, so nothing said here is posted or sent "
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
_HELD_READ_MSG = (
    "this conversation is in a confidential channel, so nothing outside it is read here. "
    "Tell the caller. Do not retry."
)
_OWN_AGENTS_MSG = (
    "this channel is confidential: only its own agents read it, so nothing was read. "
    "Tell the caller. Do not retry."
)
_AMBIGUOUS_HOLD_MSG = (
    "this agent is answering in more than one confidential channel at once, so daimon "
    "can't tell which one this call belongs to and did nothing. Tell the caller to "
    "try again once one of those answers is done."
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
    policy: TenantAccessPolicy | None = None,
    agent: AgentRef | None = None,
) -> None:
    """Raise ToolError when the tenant policy forbids this post (`authorize(POST)`).

    Tidy supplies a policy loaded under the shared policy-write lock and agent facts
    resolved before locking; supplying both avoids platform lookups in the guard (a
    running turn's hold is still read from the database).

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
    policy = policy if policy is not None else await load_channel_policy(runtime, auth)
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
    if not any_confidential(policy) and (place.own_dm or not any_pinned(policy)):
        return
    agent = agent if agent is not None else await _executing_agent(runtime, auth, policy)
    # A running turn's hold only narrows: it never grants what `origin` would not.
    running = await _held_origin(runtime, auth, policy, agent, None)
    hold = isolation_hold(policy, agent, turn_origin_place(running)) if running else None
    if hold is not None and confidential_channel_of(policy, channel_id, parent_channel_id) != hold:
        record_authz_denial(Action.POST, "channel_isolated")
        raise ToolError(_HELD_SEND_MSG)
    decision = authorize(
        policy, subject=subject, action=Action.POST, agent=agent, place=place, origin=origin
    )
    if not decision:
        record_authz_denial(Action.POST, decision.reason)
    if decision.reason == "channel_isolated":
        if confidential_channel_of(policy, channel_id, parent_channel_id):
            raise ToolError(_ISOLATED_WRITE_MSG)
        if _origin_isolated(policy, origin):
            raise ToolError(_HELD_SEND_MSG)
    if not decision:
        raise ToolError(_PINNED_SEND_MSG)


def _origin_isolated(policy: TenantAccessPolicy, origin: Place | None) -> bool:
    return origin is not None and (
        confidential_channel_of(policy, origin.channel_id, origin.parent_channel_id) is not None
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
    if executing is None or not (any_pinned(policy) or any_confidential(policy)):
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
    agent = await _executing_agent(runtime, auth, policy)
    decision = authorize(
        policy,
        subject=mcp_subject(auth),
        action=Action.DIRECT_MESSAGE,
        agent=agent,
        recipient_id=recipient_id,
        origin=await _running_hold(runtime, auth, policy, agent),
    )
    if not decision:
        record_authz_denial(Action.DIRECT_MESSAGE, decision.reason)
    if decision.reason == "agent_unresolved":
        raise ToolError(_PINNED_SEND_MSG)
    if decision.reason == "channel_isolated":
        raise ToolError(
            "this agent is held to a confidential channel, so it sends no direct messages: "
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
    refusal = await _held_refusal(runtime, auth, Action.CREATE_AGENT, origin)
    if refusal == "origin_missing":
        raise ToolError(
            "create_agent needs a verified origin_context_id from a chat turn while a "
            "channel in this workspace is confidential, and this call has none. Nothing was "
            "created. Tell the caller to create the agent from a chat conversation. "
            "Do not retry."
        )
    if refusal is not None:
        raise ToolError(
            "this conversation is held to a confidential channel, so it creates no agents: a "
            "new agent would answer outside it. Tell the caller to create it from a "
            "conversation outside that channel. Nothing was created. Do not retry."
        )


async def require_publishable(
    runtime: McpRuntime, auth: AuthIdentity, *, origin_context_id: str | None
) -> None:
    """Publishing a report, notebook or blog puts content behind a link whoever
    holds it opens, so it is refused wherever a post outside the channel is
    (`authorize(PUBLISH)`), admins included: a pinned agent, an isolated
    channel's own agent, or a call held to one. While a channel is isolated a
    chat turn naming no verified origin is refused too, as for create_agent. An
    agent key's call is held by its bound channel, never by an origin it names."""
    await _require_unheld(
        runtime, auth, origin_context_id, what="publishing", nothing="Nothing was published."
    )


async def require_reader_source_publishable(
    runtime: McpRuntime, auth: AuthIdentity, source: BetaManagedAgentsAgent
) -> None:
    """A published reader answers as its source agent for whoever holds the link,
    so an isolated channel's own agent's reader is refused, admins included. A
    pinned source is the pin write rule's (`require_pin_write_access`)."""
    policy = await load_channel_policy(runtime, auth)
    if not any_confidential(policy):
        return
    decision = authorize(
        policy,
        subject=mcp_subject(auth),
        action=Action.PUBLISH,
        agent=build_agent_ref(source.name, source.metadata),
    )
    if decision.reason == "channel_isolated":
        record_authz_denial(Action.PUBLISH, decision.reason)
        raise ToolError(
            f"'{source.name}' is a confidential channel's own agent, so its reader can't be "
            "published: a link reaches whoever holds it. Tell the caller. Nothing was "
            "published. Do not retry."
        )


async def require_identity_changeable(
    runtime: McpRuntime, auth: AuthIdentity, *, origin_context_id: str | None
) -> None:
    """Daimon's server nickname and avatar show in every channel, so one
    channel's agent changes them nowhere: refused as `require_publishable` is."""
    await _require_unheld(
        runtime,
        auth,
        origin_context_id,
        what="changing daimon's server-wide name or avatar",
        nothing="Nothing was changed.",
    )


async def _require_unheld(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin_context_id: str | None,
    *,
    what: str,
    nothing: str,
) -> None:
    origin = (
        None
        if auth.agent_id is not None
        else await get_verified_origin(runtime, auth, origin_context_id)
    )
    place = turn_origin_place(origin) if origin is not None else None
    refusal = await _held_refusal(runtime, auth, Action.PUBLISH, place)
    if refusal == "origin_missing":
        raise ToolError(
            f"{what} needs this turn's origin_context_id while a channel in this "
            f"workspace is confidential, and this call has none. {nothing} Pass it and retry once."
        )
    if refusal == "agent_pinned":
        raise ToolError(
            f"this agent is pinned to its own channels, so {what} is refused: it shows "
            f"outside them. Tell the caller. {nothing} Do not retry."
        )
    if refusal == "agent_unresolved":
        raise ToolError(
            f"this conversation's agent could not be found, so daimon can't tell which "
            f"channel it is held to and {what} is refused. Tell the caller. {nothing}"
        )
    if refusal is not None:
        raise ToolError(
            f"this conversation is held to a confidential channel, so {what} is refused: it "
            f"would carry the channel outside. Tell the caller. {nothing} Do not retry."
        )


async def _held_refusal(
    runtime: McpRuntime, auth: AuthIdentity, action: Action, origin: Place | None
) -> str | None:
    """Why `action` is refused for a call held to a channel, audited; None when allowed.

    An identity with no executing agent (the operator, a person's own hub
    token) is held nowhere."""
    if auth.chat_agent_id is None and auth.agent_id is None:
        return None
    policy = await load_channel_policy(runtime, auth)
    if not (any_confidential(policy) or any_pinned(policy)):
        return None
    agent = await _executing_agent(runtime, auth, policy)
    decision = authorize(
        policy,
        subject=mcp_subject(auth),
        action=action,
        agent=agent,
        origin=await _running_hold(runtime, auth, policy, agent)
        or origin
        or (mcp_place(auth) if token_channel_id(auth) is not None else None),
    )
    if not decision:
        record_authz_denial(action, decision.reason)
        return decision.reason
    if any_confidential(policy) and auth.agent_id is None and origin is None:
        record_authz_denial(action, "origin_missing")
        return "origin_missing"
    return None


class SealedChannelError(ToolError):
    """A read of a sealed channel from outside it. Not an access problem the
    caller can fix by connecting an account, so no connect hint follows it."""


@dataclass(frozen=True)
class ChannelReadPolicy:
    """The tenant policy plus the channels the calling turn runs in.

    `agent` is the executing agent while the tenant isolates a channel: only
    that channel's own agents read it, and they read nothing else. `origin` is
    the verified turn origin the ids came from, if any; `origin_place` where it
    (or a channel-bound key) runs, which holds a call from inside an isolated
    channel to it.
    """

    policy: TenantAccessPolicy
    origin_channel_ids: frozenset[str] = frozenset()
    agent: AgentRef = field(default_factory=AgentRef.none)
    origin: TurnOriginRow | None = None
    origin_place: Place | None = None

    def _decide(self, channel_id: str, parent_channel_id: str | None) -> Decision:
        return authorize(
            self.policy,
            subject=Subject(),
            action=Action.READ_CHANNEL,
            agent=self.agent,
            place=Place(channel_id=channel_id, parent_channel_id=parent_channel_id),
            origin_channel_ids=self.origin_channel_ids,
            origin=self.origin_place,
        )

    def allows(self, channel_id: str, parent_channel_id: str | None = None) -> bool:
        return bool(self._decide(channel_id, parent_channel_id))

    def lists(self, channel_id: str, parent_channel_id: str | None = None) -> bool:
        """Whether a channel list may name this channel: a call held to an isolated
        channel names only it. A seal hides messages, not the name."""
        held = isolation_hold(self.policy, self.agent, self.origin_place)
        return held is None or held == confidential_channel_of(
            self.policy, channel_id, parent_channel_id
        )

    def require(self, channel_id: str, parent_channel_id: str | None = None) -> None:
        """Raise ToolError for a sealed target outside the calling turn, or any
        target outside the isolated channel the call is held to.

        Call after the platform's caller-permission check."""
        decision = self._decide(channel_id, parent_channel_id)
        if decision:
            return
        if not self.lists(channel_id, parent_channel_id):
            raise SealedChannelError(_HELD_READ_MSG)
        if decision.reason == "channel_isolated":
            raise SealedChannelError(_OWN_AGENTS_MSG)
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
        if any_confidential(policy)
        else AgentRef.none()
    )
    bound = token_channel_id(auth)
    if bound is not None:
        return ChannelReadPolicy(
            policy, frozenset({bound}), agent, origin_place=Place(channel_id=bound)
        )
    # Every isolated channel is sealed, so an origin that holds a call is resolved.
    if not (any_sealed(policy) or resolve_without_seals):
        return ChannelReadPolicy(policy, agent=agent)
    origin = await get_verified_origin(runtime, auth, origin_context_id)
    origin = await _held_origin(runtime, auth, policy, agent, origin)
    if origin is None:
        return ChannelReadPolicy(policy, agent=agent)
    inside = {origin.parent_channel_id, origin.thread_id}
    if auth.platform == "slack":
        # A Slack thread is sealed by its channel_id:thread_ts form.
        inside.add(f"{origin.parent_channel_id}:{origin.thread_id}")
    return ChannelReadPolicy(
        policy, frozenset(inside), agent, origin, origin_place=turn_origin_place(origin)
    )


async def require_within_hold(
    runtime: McpRuntime,
    auth: AuthIdentity,
    channel_id: str,
    parent_channel_id: str | None = None,
    *,
    origin_context_id: str | None = None,
) -> None:
    """Refuse a target outside the isolated channel the call is held to, as a read is."""
    read = await load_read_policy(runtime, auth, origin_context_id=origin_context_id)
    if not read.lists(channel_id, parent_channel_id):
        raise ToolError(_HELD_READ_MSG)


async def _running_hold(
    runtime: McpRuntime, auth: AuthIdentity, policy: TenantAccessPolicy, agent: AgentRef
) -> Place | None:
    """Where a chat turn of this account and agent running in an isolated channel
    is, whatever origin the call names; it only narrows (`_held_origin`)."""
    running = await _held_origin(runtime, auth, policy, agent, None)
    return turn_origin_place(running) if running is not None else None


async def _held_origin(
    runtime: McpRuntime,
    auth: AuthIdentity,
    policy: TenantAccessPolicy,
    agent: AgentRef,
    passed: TurnOriginRow | None,
) -> TurnOriginRow | None:
    """The origin that holds a chat-turn call, else `passed`.

    A chat token is shared by every turn of its account and agent and names
    none, so a held turn could leave out its origin, or name another. Any of
    them running in an isolated channel holds the call there; two in
    different ones refuse it.
    """
    if (
        not any_confidential(policy)
        or auth.chat_agent_id is None
        or auth.agent_id is not None
        or auth.platform is None
        # An agent of its own channel is held there already.
        or isolation_hold(policy, agent, None) is not None
        or (passed is not None and isolation_hold(policy, agent, turn_origin_place(passed)))
    ):
        return passed
    async with runtime.session_factory() as session:
        running = await list_active_origins(
            session,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            platform=auth.platform,
            now=datetime.now(UTC),
        )
    held: dict[str, TurnOriginRow] = {}
    for origin in running:
        responder = derive_agent_uuid(
            tenant_id=auth.tenant_id, ma_agent_id=origin.responder_ma_agent_id
        )
        hold = isolation_hold(policy, agent, turn_origin_place(origin))
        if responder == auth.chat_agent_id and hold is not None:
            held.setdefault(hold, origin)
    if len(held) > 1:
        raise ToolError(_AMBIGUOUS_HOLD_MSG)
    return next(iter(held.values()), passed)


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
