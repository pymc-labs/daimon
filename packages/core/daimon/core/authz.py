"""Authorization decisions for tenant, agent, channel, and tool operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Literal, cast
from uuid import UUID

from daimon.core.access_policy import DM_SCOPE_PREFIX, TenantAccessPolicy, is_invoker_allowed
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_READER_OF,
    MA_METADATA_KEY_READER_SOURCE,
)
from daimon.core.permissions import (
    AgentPermissions,
    ChannelPermissions,
    agent_permissions,
    any_agent_rules,
    any_own_readers,
    channel_permissions,
    crosses_home,
    held_to,
    limited_under,
    outside_runs_in,
    post_refusal,
    readable_from,
    ruled_agents,
    run_refusal,
    session_homes,
    session_readable_from,
)
from daimon.core.turn_hold import TurnHold
from mux.contracts.ids import Scope, ThreadRef


class Action(StrEnum):
    """What the subject wants to do."""

    # A platform turn's caller gates: the reply target's writers, then the
    # invoker allowlist. Runs before the agent is known.
    START_TURN = "start_turn"
    SPAWN_WORKER = "spawn_worker"
    # Run a resolved agent here (a turn, a routine fire, a handoff binding).
    RUN_AGENT = "run_agent"
    # Save (create or update) a routine for an agent: RUN_AGENT at the
    # routine's destination, but only once a non-empty name of the agent has
    # a rule (an empty name never gives a routine one). `Place.channel_id`
    # is the destination channel (None for a Discord thread or no
    # destination) and `parent_channel_id` the channel it posts into.
    SAVE_ROUTINE = "save_routine"
    # Change what an agent reaches: keys, connectors, MCP servers, prompt,
    # tools, skills, repo binding.
    CONFIGURE = "configure"
    # Post into a channel, thread or conversation as the executing agent. With
    # no agent (`AgentRef.none()`) only the writers rule is decided: an adapter's own
    # notice around a turn, or a routine's delivery post.
    POST = "post"
    # Run or deliver a routine on its creator's behalf: only while the creator
    # may still start a turn (the invoker allowlist and optional admin bypass). A
    # routine with no recorded creator speaks for no one.
    ACT_FOR_CREATOR = "act_for_creator"
    # Send a direct message to a workspace member as the executing agent.
    DIRECT_MESSAGE = "direct_message"
    # Create an agent from the calling turn; `agent` is the one it executes as.
    CREATE_AGENT = "create_agent"
    # Publish a report, notebook or blog, or change daimon's server-wide
    # identity, from the calling turn: seen outside every channel, so judged as
    # a post outside them, unless the requester approved it (`Request.approved`).
    PUBLISH = "publish"
    # Change what a possibly shared agent runs or reaches: a spec edit, an
    # attachment replace/remove, or a posted-token contribution
    # (`daimon.core.operation_policy`). Decided per `Request.operation_family`
    # on `Request.reach`, each family in its own fixed order.
    CHANGE_SHARED_AGENT = "change_shared_agent"
    # Copy an agent.
    FORK = "fork"
    # Make an agent the default of one channel (`Place.channel_id`).
    BIND_CHANNEL_DEFAULT = "bind_channel_default"
    # Mint a coding-tool token for an agent, bound to `Place.channel_id` (no
    # channel: unbound).
    MINT_CODING_TOKEN = "mint_coding_token"
    # Set or clear the environment of one channel (`Place.channel_id`; None is
    # the workspace default). `open_network` says the environment it leaves
    # the channel in has unrestricted networking.
    SET_CHANNEL_ENVIRONMENT = "set_channel_environment"
    # Set one channel's rule (`Place.channel_id`).
    SET_CHANNEL_RULE = "set_channel_rule"
    # Set one agent's rule.
    SET_AGENT_RULE = "set_agent_rule"
    # Archive the copy a channel kept to its own agents was given, as it opens.
    ARCHIVE_CHANNEL_COPY = "archive_channel_copy"
    # Set, clear or raise the budget of one channel (`Place.channel_id`).
    SET_CHANNEL_BUDGET = "set_channel_budget"
    # Add or remove the extra skills one channel's turns run with (`Place.channel_id`).
    SET_CHANNEL_SKILLS = "set_channel_skills"
    # Set or clear an agent's third-party MCP tool rule: current human admins only.
    SET_AGENT_TOOL_RULE = "set_agent_tool_rule"
    # Set or clear one agent's tool rule for a parent channel.
    SET_CHANNEL_AGENT_TOOL_RULE = "set_channel_agent_tool_rule"
    # Turn built-in sandbox tools on or off for one routine's own runs:
    # current human admins only, never the routine's owner as such.
    SET_ROUTINE_SANDBOX_TOOLS = "set_routine_sandbox_tools"
    # Hand a thread's conversation to another agent (`Place` is the thread).
    # Writers and the invoker allowlist as START_TURN, then RUN_AGENT on the
    # handoff surface, then who may bring that agent here: anyone when it is
    # scoped to this channel (`Request.answers_here`, a rule naming it, or one
    # of the channel's own agents); otherwise a server admin unless the
    # place's readers are limited.
    HAND_OFF = "hand_off"
    # Read a channel's content.
    READ_CHANNEL = "read_channel"
    # Read or continue a recorded session's transcript.
    READ_SESSION = "read_session"
    # Add to a recorded session's transcript (continue_turn, ask(handle)).
    CONTINUE_SESSION = "continue_session"


class Surface(StrEnum):
    """Where the result of the action lands, which decides the admin exemption."""

    CHANNEL = "channel"  # a channel or thread other members see
    DM = "dm"  # a platform DM with the bot: only the person sees it
    HUB = "hub"  # a person's own hub session: only they see it
    AGENT_CHAT = "agent_chat"  # an agent-key or bearer call
    ROUTINE = "routine"  # a scheduled run posting into a channel
    HANDOFF = "handoff"  # a task handed to another agent in a thread
    FLEET = "fleet"
    CONFIG = "config"  # a configuration write (request tool, form, OAuth, direct tool)


#: Surfaces whose output reaches only the acting admin.
_ADMIN_ONLY_SURFACES = frozenset({Surface.DM, Surface.HUB})


@dataclass(frozen=True)
class FleetFacts:
    """Host-attested root execution, never a worker request's identity fields."""

    scope: Scope
    root: ThreadRef
    origin_id: UUID
    account_id: UUID
    run_id: UUID
    place: Place
    depth: int = 0
    enabled: bool = False
    active: bool = False


@dataclass(frozen=True)
class Subject:
    """Who is acting.

    ``is_admin`` is the caller's trusted admin signal for this surface (see the
    module docstring); a caller that never trusts an admin -- an agent key, a
    bearer token -- passes False. ``platform_user_id`` is None for a token with
    no person behind it.
    """

    is_admin: bool = False
    platform_user_id: str | None = None
    # The caller holds an agent-scoped key: never exempt as an admin, whoever
    # minted it, because the key is long-lived and its role may be stale.
    via_agent_key: bool = False
    role_ids: tuple[str, ...] = ()
    verified_membership: bool | None = None
    # The requester belongs to another organisation (or the platform could not
    # say): never an admin, and refused everything outside the held channel.
    client: bool = False


@dataclass(frozen=True)
class AgentRef:
    """The agent the action is about.

    ``names`` holds every name a pin may be keyed by. ``resolved`` is False
    when the target should exist but couldn't be found (a vanished or legacy
    target), which fails closed under any pin. ``present`` is False when there
    is no agent at all -- the deployment operator's own call.
    """

    names: tuple[str | None, ...] = ()
    resolved: bool = True
    present: bool = True
    # What the agent rules say of it. Filled in by `authorize` from the
    # policy; never pass it.
    permissions: AgentPermissions = field(default_factory=AgentPermissions)

    @classmethod
    def of(cls, *names: str | None) -> AgentRef:
        return cls(names=tuple(names))

    @classmethod
    def unresolved(cls) -> AgentRef:
        return cls(resolved=False)

    @classmethod
    def none(cls) -> AgentRef:
        return cls(present=False)


@dataclass(frozen=True)
class Place:
    """Where the action happens or lands.

    ``channel_id`` is the conversation (a thread's own id for a thread);
    ``parent_channel_id`` is the channel a thread sits under. Both None is no
    channel: a DM or a headless call. ``own_dm`` marks the requester's own 1:1
    conversation with the bot (the caller has verified membership).
    """

    channel_id: str | None = None
    parent_channel_id: str | None = None
    category_id: str | None = None
    category_unresolved: bool = False
    own_dm: bool = False
    # A setup conversation's thread: the built-in agent answers it to set the
    # channel up, so it runs there even in a channel kept to its own agents.
    setup_thread: bool = False
    # A thread whose parent channel isn't known (a routine saved before its
    # parent was recorded): while some channel's readers are own it may lie in one.
    parent_unresolved: bool = False
    # What the channel rules say here. Filled in by `authorize` from the
    # policy; never pass it.
    permissions: ChannelPermissions = field(default_factory=ChannelPermissions)

    @classmethod
    def from_origin(
        cls, *, parent_channel_id: str | None, thread_id: str | None, setup_thread: bool = False
    ) -> Place:
        """The place a recorded turn origin stands for.

        A private DM conversation's scope id (``dm:…``) stands in for its
        thread id; a DM runs in no workspace channel, so it is outside every
        agent rule, as ``admit(is_dm=True)`` treats it.
        """
        if thread_id is not None and thread_id.startswith(DM_SCOPE_PREFIX):
            return cls()
        return cls(
            channel_id=thread_id or parent_channel_id,
            parent_channel_id=parent_channel_id,
            setup_thread=setup_thread,
        )


@dataclass(frozen=True)
class SessionFacts:
    """What a recorded session says about where it ran (READ_SESSION only).

    ``owned`` is whether the session belongs to the caller's own account;
    ``private`` marks a DM conversation (the private-DM stamp, a ``dm:``
    scope, a Slack IM or a Teams personal chat), which no one else ever reads.
    ``channel`` / ``thread`` are the stamps it was created with,
    ``seal_ids`` every id that limited its readers, and ``legacy_thread_id`` the thread
    an unstamped (pre-stamp) session ran on, if any.
    """

    channel: str | None = None
    thread: str | None = None
    seal_ids: frozenset[str] = frozenset()
    legacy_thread_id: str | None = None
    owned: bool = True
    private: bool = False


OperationFamily = Literal["spec", "attachment", "posted_token"]


@dataclass(frozen=True)
class AgentReach:
    """Facts about the agent used by mutation policy."""

    managed: bool = False
    reachable: bool = False
    # The caller is the agent's recorded owner (`daimon.core.stores.agent_owners`).
    owned: bool = False


DenyReason = Literal[
    "fleet_origin_required",
    "worker_spawn_forbidden",
    "worker_depth_exceeded",
    "writers_none",
    "invoker_not_allowed",
    "runs_elsewhere",
    "agent_unresolved",
    "dm_recipient_not_requester",
    "admin_required",
    "agent_has_rule",
    "not_a_reader",
    "not_owner",
    "managed_agent",
    "own_agents_only",
    "needs_approval",
    # The requester is from another organisation: the action is not theirs to take.
    "client_requester",
    # The target lies outside the channel the turn is held to.
    "outside_hold",
    # A client's call named no origin channel to be held to.
    "origin_required",
]


@dataclass(frozen=True)
class Decision:
    """ALLOW, or DENY with the reason the caller renders into its own copy."""

    allowed: bool
    reason: DenyReason | None = None

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = Decision(True)


def _deny(reason: DenyReason) -> Decision:
    return Decision(False, reason)


@dataclass(frozen=True)
class Request:
    """One authorization question. Built by `authorize`'s keyword form."""

    subject: Subject
    action: Action
    surface: Surface = Surface.CHANNEL
    agent: AgentRef = field(default_factory=AgentRef.none)
    place: Place = field(default_factory=Place)
    recipient_id: str | None = None
    origin_channel_ids: frozenset[str] = frozenset()
    # Where the calling turn runs, from a verified turn origin (POST,
    # DIRECT_MESSAGE, SAVE_ROUTINE); None when the call named none.
    origin: Place | None = None
    session: SessionFacts | None = None
    operation_family: OperationFamily | None = None
    reach: AgentReach | None = None
    open_network: bool = False
    # HAND_OFF: the channel/workspace cascade already sends this place's
    # parent channel to the agent.
    answers_here: bool = False
    # HAND_OFF: every id that limited the readers of a live session in the
    # thread, as the sessions recorded it. A limit lifted since still counts.
    recorded_seal_ids: frozenset[str] = frozenset()
    # PUBLISH: the requester approved this very call on a card, as the caller
    # verified (`daimon.core.publish_gate`).
    approved: bool = False
    fleet: FleetFacts | None = None
    # The channel the calling turn is held to; None for an unheld call.
    hold: TurnHold | None = None


#: What a client requester may never do, whatever its place or surface.
_CLIENT_REFUSED_ACTIONS = frozenset(
    {
        Action.DIRECT_MESSAGE,
        Action.SAVE_ROUTINE,
        Action.ACT_FOR_CREATOR,
        Action.CREATE_AGENT,
        Action.CONFIGURE,
        Action.CHANGE_SHARED_AGENT,
        Action.FORK,
        Action.BIND_CHANNEL_DEFAULT,
        Action.MINT_CODING_TOKEN,
        Action.SET_CHANNEL_ENVIRONMENT,
        Action.SET_CHANNEL_RULE,
        Action.SET_AGENT_RULE,
        Action.SET_CHANNEL_BUDGET,
        Action.SET_CHANNEL_SKILLS,
        Action.SET_AGENT_TOOL_RULE,
        Action.SET_CHANNEL_AGENT_TOOL_RULE,
        Action.ARCHIVE_CHANNEL_COPY,
        Action.SPAWN_WORKER,
    }
)

#: Surfaces a client may never start a turn or run an agent on: everything but a channel.
_CLIENT_REFUSED_SURFACES = frozenset(
    {Surface.DM, Surface.HUB, Surface.AGENT_CHAT, Surface.ROUTINE, Surface.FLEET}
)

#: What a held turn may not do at all, client or not.
_HOLD_REFUSED_ACTIONS = frozenset(
    {Action.DIRECT_MESSAGE, Action.SAVE_ROUTINE, Action.CREATE_AGENT, Action.SPAWN_WORKER}
)


#: What a client may do only from a named origin channel, so it needs a hold.
_ORIGIN_BOUND_ACTIONS = frozenset(
    {Action.READ_CHANNEL, Action.POST, Action.READ_SESSION, Action.CONTINUE_SESSION}
)


def _inside_hold(hold: TurnHold, at: Place) -> bool:
    return not at.own_dm and hold.contains(at.channel_id, at.parent_channel_id)


def _session_inside_hold(hold: TurnHold, facts: SessionFacts) -> bool:
    if facts.private:
        return False
    if facts.thread is not None:
        return hold.contains(facts.thread, facts.channel)
    return hold.contains(facts.channel if facts.channel is not None else facts.legacy_thread_id)


def _client_refusal(req: Request) -> Decision | None:
    if req.action in _CLIENT_REFUSED_ACTIONS:
        return _deny("client_requester")
    if req.action in (Action.START_TURN, Action.RUN_AGENT) and (
        req.surface in _CLIENT_REFUSED_SURFACES
    ):
        return _deny("client_requester")
    if req.action is Action.PUBLISH and not req.approved:
        return _deny("needs_approval")
    if req.hold is None and req.action in _ORIGIN_BOUND_ACTIONS:
        return _deny("origin_required")
    return None


def _hold_refusal(req: Request, hold: TurnHold) -> Decision | None:
    if req.action in _HOLD_REFUSED_ACTIONS:
        return _deny("outside_hold")
    if req.action in (Action.READ_CHANNEL, Action.POST):
        return None if _inside_hold(hold, req.place) else _deny("outside_hold")
    if req.action is Action.PUBLISH and not req.approved:
        return _deny("needs_approval")
    if req.action in (Action.READ_SESSION, Action.CONTINUE_SESSION):
        facts = req.session or SessionFacts()
        return None if _session_inside_hold(hold, facts) else _deny("outside_hold")
    return None


def _held_refusal(req: Request) -> Decision | None:
    """A client's or a held turn's refusal, decided before any admin exemption
    and before every no-rules ALLOW, so no policy can relax it."""
    if req.subject.client:
        refused = _client_refusal(req)
        if refused is not None:
            return refused
    if req.hold is not None:
        return _hold_refusal(req, req.hold)
    return None


def _outside_rule(agent: AgentRef, place: Place) -> bool:
    return outside_runs_in(agent.permissions, place.channel_id, place.parent_channel_id)


def _origin_permissions(req: Request) -> ChannelPermissions | None:
    return req.origin.permissions if req.origin is not None else None


def _held_to(agent: AgentRef, origin: Place | None) -> str | None:
    return held_to(agent.permissions, origin.permissions if origin is not None else None)


def holds_limited_readers(policy: TenantAccessPolicy, channel: str, *places: Place | None) -> bool:
    """Whether `channel`, or a thread under it, has limited readers: a Slack
    ``channel:ts`` by its id, a Discord thread only when one of `places` names it."""
    threads = [
        at.channel_id
        for at in places
        if at is not None and at.parent_channel_id == channel and at.channel_id is not None
    ]
    return limited_under(policy, channel, threads)


def _scoped_here(req: Request) -> bool:
    """The agent belongs in this place without anyone's say-so.

    The channel answers with it, its rule names this channel, or it is one of
    the channel's own agents.
    """
    agent, place = req.agent, req.place
    if req.answers_here:
        return True
    if (
        place.channel_id is not None
        and agent.permissions.runs_in
        and not _outside_rule(agent, place)
    ):
        return True
    here = place.permissions.home
    return here is not None and agent.permissions.home == here


def _decide_hand_off(policy: TenantAccessPolicy, req: Request) -> Decision:
    subject = req.subject
    started = _decide(policy, replace(req, action=Action.START_TURN))
    if not started:
        return started
    if not req.agent.present:
        return _deny("agent_unresolved")
    # The handoff surface is shared, so no admin exemption from the agent rule.
    ran = _decide(policy, replace(req, action=Action.RUN_AGENT, surface=Surface.HANDOFF))
    if not ran:
        return ran
    if _scoped_here(req):
        return ALLOW
    if subject.is_admin and not subject.via_agent_key:
        return ALLOW
    return _deny("admin_required")


def _writers_none(place: Place) -> bool:
    return place.permissions.writers == "none"


def _decide_shared_agent_change(req: Request) -> Decision:
    """The shared-agent mutation table (`daimon.core.operation_policy`).

    A posted-token write always passes. A spec edit refuses a managed agent even
    for an admin, then passes an admin or the agent's owner; an attachment write
    passes an admin before the managed check, then the owner. Every other target
    needs an admin.
    """
    reach = req.reach or AgentReach()
    owner = reach.owned and not req.subject.via_agent_key and not req.subject.client
    if req.operation_family == "posted_token":
        return ALLOW
    if req.operation_family == "spec":
        if reach.managed:
            return _deny("managed_agent")
        if req.subject.is_admin or owner:
            return ALLOW
    else:
        if req.subject.is_admin:
            return ALLOW
        if reach.managed:
            return _deny("managed_agent")
        if owner:
            return ALLOW
    return _deny("admin_required")


def _decide(policy: TenantAccessPolicy, req: Request) -> Decision:
    if req.subject.client or req.hold is not None:
        # No administrator authority applies to a client or a held request.
        req = replace(req, subject=replace(req.subject, is_admin=False))
        refused = _held_refusal(req)
        if refused is not None:
            return refused
    subject, agent, place = req.subject, req.agent, req.place
    admin_only_surface = req.surface in _ADMIN_ONLY_SURFACES

    if req.action is Action.SPAWN_WORKER:
        fleet = req.fleet
        if req.surface is not Surface.FLEET or fleet is None:
            return _deny("fleet_origin_required")
        if fleet.enabled is not True or fleet.active is not True or place.setup_thread:
            return _deny("worker_spawn_forbidden")
        if type(fleet.depth) is not int or fleet.depth != 0:
            return _deny("worker_depth_exceeded")
        if (
            fleet.scope.is_platform
            or fleet.scope.is_legacy_host_authorized
            or not fleet.scope.principal_id.strip()
            or not fleet.scope.authorization_id.strip()
            or not isinstance(cast(object, fleet.origin_id), UUID)
            or not isinstance(cast(object, fleet.run_id), UUID)
            or str(fleet.account_id) != fleet.scope.account_id
            or fleet.scope.tenant_id != fleet.root.channel.tenant_id
            # authorize refreshes the policy-derived permissions. Every other
            # admission fact must still be the authentic root's complete Place.
            or replace(place, permissions=fleet.place.permissions) != fleet.place
            or place.channel_id != fleet.root.thread_id
            or place.parent_channel_id != fleet.root.channel.channel_id
            or not agent.present
            or not agent.resolved
        ):
            return _deny("fleet_origin_required")
        started = _decide(policy, replace(req, action=Action.START_TURN))
        if not started:
            return started
        return _decide(policy, replace(req, action=Action.RUN_AGENT))

    if req.action is Action.START_TURN:
        if _writers_none(place):
            return _deny("writers_none")
        if subject.platform_user_id is not None and not is_invoker_allowed(
            policy,
            external_user_id=subject.platform_user_id,
            is_admin=subject.is_admin,
            role_ids=subject.role_ids,
            verified_membership=subject.verified_membership,
        ):
            return _deny("invoker_not_allowed")
        return ALLOW

    if req.action is Action.ACT_FOR_CREATOR:
        if subject.platform_user_id is None or not is_invoker_allowed(
            policy,
            external_user_id=subject.platform_user_id,
            is_admin=subject.is_admin,
            role_ids=subject.role_ids,
            verified_membership=subject.verified_membership,
        ):
            return _deny("invoker_not_allowed")
        return ALLOW

    if req.action is Action.RUN_AGENT:
        if (
            subject.is_admin
            and admin_only_surface
            and subject.platform_user_id is not None
            and not subject.via_agent_key
        ):
            return ALLOW
        if not agent.present:
            return ALLOW
        if not agent.resolved:
            if place.permissions.home is not None and not place.setup_thread:
                return _deny("own_agents_only")
            return _deny("agent_unresolved") if any_agent_rules(policy) else ALLOW
        refusal = run_refusal(agent.permissions, place.permissions, setup_thread=place.setup_thread)
        return ALLOW if refusal is None else _deny(refusal)

    if req.action is Action.SAVE_ROUTINE:
        here = place.permissions.home
        if here is not None and agent.permissions.home != here:
            return _deny("own_agents_only")
        # Saved from inside C: the result stays in C.
        if req.origin is not None and req.origin.permissions.home not in (
            None,
            here,
        ):
            return _deny("own_agents_only")
        ruled = ruled_agents(policy)
        if not any(name in ruled for name in agent.names if name):
            return ALLOW
        # Straight into a channel its rule names: a thread under one is
        # refused, as a fire can't always tell its parent.
        if _outside_rule(agent, Place(channel_id=place.channel_id)):
            return _deny("runs_elsewhere")
        return ALLOW

    if req.action is Action.CONFIGURE:
        if (subject.is_admin and not subject.via_agent_key) or not any_agent_rules(policy):
            return ALLOW
        if not agent.resolved:
            return _deny("agent_unresolved")
        if _outside_rule(agent, place):
            return _deny("runs_elsewhere")
        return ALLOW

    if req.action is Action.BIND_CHANNEL_DEFAULT:
        if _outside_rule(agent, place):
            return _deny("runs_elsewhere")
        if agent.present and crosses_home(agent.permissions, place.permissions):
            return _deny("own_agents_only")
        return ALLOW

    if req.action is Action.SET_CHANNEL_ENVIRONMENT:
        return ALLOW if subject.is_admin and not subject.via_agent_key else _deny("admin_required")

    if req.action in (
        Action.ARCHIVE_CHANNEL_COPY,
        Action.SET_CHANNEL_RULE,
        Action.SET_AGENT_RULE,
    ):
        # none, even on their own channel.
        if subject.is_admin and not subject.via_agent_key:
            return ALLOW
        return _deny("admin_required")

    if req.action is Action.SET_AGENT_TOOL_RULE:
        return ALLOW if subject.is_admin and not subject.via_agent_key else _deny("admin_required")

    if req.action is Action.SET_CHANNEL_AGENT_TOOL_RULE:
        return ALLOW if subject.is_admin and not subject.via_agent_key else _deny("admin_required")

    if req.action is Action.SET_ROUTINE_SANDBOX_TOOLS:
        return ALLOW if subject.is_admin and not subject.via_agent_key else _deny("admin_required")

    if req.action in (Action.SET_CHANNEL_BUDGET, Action.SET_CHANNEL_SKILLS):
        # Money, and what a shared agent may do, stay with server admins; an
        # admin's own agent key keeps the rights it always had.
        return ALLOW if subject.is_admin else _deny("admin_required")

    if req.action is Action.MINT_CODING_TOKEN:
        return ALLOW if subject.is_admin and not subject.via_agent_key else _deny("admin_required")

    if req.action is Action.POST:
        # A caller with no agent (the CLI, an operator token) is input: only
        # the writers rule stops it.
        if not agent.present:
            return _deny("writers_none") if _writers_none(place) else ALLOW
        origin = req.origin
        refusal = post_refusal(
            agent.permissions,
            place.permissions,
            origin.permissions if origin is not None else None,
            setup_origin=origin is not None and origin.setup_thread,
        )
        if refusal == "writers_none":
            return _deny("writers_none")
        if refusal == "own_agents_only":
            return _deny("own_agents_only" if agent.resolved else "agent_unresolved")
        if place.own_dm or not any_agent_rules(policy):
            return ALLOW
        if not agent.resolved:
            return _deny("agent_unresolved")
        return ALLOW if refusal is None else _deny(refusal)

    if req.action is Action.DIRECT_MESSAGE:
        if not agent.present:
            return ALLOW
        recipients = agent.permissions.direct_messages(_origin_permissions(req))
        if recipients == "none":
            return _deny("own_agents_only")
        if not any_agent_rules(policy):
            return ALLOW
        if not agent.resolved:
            return _deny("agent_unresolved")
        if recipients == "requester" and req.recipient_id != subject.platform_user_id:
            return _deny("dm_recipient_not_requester")
        return ALLOW

    if req.action is Action.CREATE_AGENT:
        # A new agent is nobody's own: what a call held to C wrote into it would
        # answer outside C. An unresolved agent may be C's own, so it fails closed.
        if not agent.resolved and any_own_readers(policy):
            return _deny("agent_unresolved")
        if not agent.permissions.creates_agents(_origin_permissions(req)):
            return _deny("own_agents_only")
        return ALLOW

    if req.action is Action.PUBLISH:
        # Wherever a post outside the channel is refused, admins included, the
        # requester's own approval is needed: a link is read by whoever holds
        # it, never only the acting admin.
        if not agent.resolved and (any_agent_rules(policy) or any_own_readers(policy)):
            return _deny("agent_unresolved")
        if agent.permissions.publishes(_origin_permissions(req)) or req.approved:
            return ALLOW
        return _deny("needs_approval")

    if req.action is Action.CHANGE_SHARED_AGENT:
        return _decide_shared_agent_change(req)

    if req.action is Action.HAND_OFF:
        return _decide_hand_off(policy, req)

    if req.action is Action.FORK:
        if not subject.is_admin or subject.via_agent_key:
            return _deny("admin_required")
        if not agent.permissions.may_be_copied:
            return _deny("agent_has_rule")
        return ALLOW

    if req.action is Action.READ_CHANNEL:
        # Held to C, a read stays in C as a post does: nothing read elsewhere,
        # a prompt injected in C included, can be carried back into C.
        held = _held_to(agent, req.origin) if agent.present else None
        if held is not None and place.permissions.home != held:
            return _deny("own_agents_only")
        if place.channel_id is None:
            return ALLOW
        if not readable_from(
            policy, req.origin_channel_ids, place.channel_id, place.parent_channel_id
        ):
            return _deny("not_a_reader")
        if (
            place.permissions.home is not None
            and agent.present
            and agent.permissions.home != place.permissions.home
        ):
            return _deny("own_agents_only")
        return ALLOW

    # Action.READ_SESSION / Action.CONTINUE_SESSION
    facts = req.session or SessionFacts()
    # Admins are trusted to READ any channel conversation of the agent from
    # their own hub session, limited or not, anyone's -- never another
    # person's private DM, and never to continue it (a follow-up would join
    # the channel's session). Their own sessions, DMs included, are read
    # without the readers filter: ownership was already proven by the caller.
    hub_read = req.action is Action.READ_SESSION and req.surface is Surface.HUB
    admin_hub_read = (
        subject.is_admin
        and subject.platform_user_id is not None
        and not subject.via_agent_key
        and (
            facts.owned
            or (
                not facts.private
                and (facts.channel is not None or facts.legacy_thread_id is not None)
            )
        )
    )
    if hub_read and admin_hub_read:
        return ALLOW
    if not facts.owned:
        return _deny("not_owner")
    if not session_readable_from(
        policy,
        req.origin_channel_ids,
        channel=facts.channel,
        thread=facts.thread,
        seal_ids=facts.seal_ids,
        legacy_thread_id=facts.legacy_thread_id,
    ):
        return _deny("not_a_reader")
    lies_in = session_homes(
        policy, channel=facts.channel, thread=facts.thread, seal_ids=facts.seal_ids
    )
    # Held to C, only a session that ran in C is read, as for READ_CHANNEL.
    held = _held_to(agent, req.origin) if agent.present else None
    if held is not None and held not in lies_in:
        return _deny("own_agents_only")
    # Only C's own agents read what was said in C, even from a turn inside it
    # (the built-in answering C's setup thread is not one of them).
    if any(channel != agent.permissions.home for channel in lies_in):
        return _deny("own_agents_only")
    return ALLOW


_READER_SUFFIX = "-reader"


def agent_names(name: str | None, metadata: Mapping[str, str]) -> tuple[str | None, ...]:
    """Every name an agent rule may be keyed by: its MA name and its config name.

    A published report's reader variant answers as its source agent, so it
    also carries the source's names (stamped as `daimon_reader_source`; a
    reader written before that stamp falls back to its name without the
    ``-reader`` suffix): a rule on the source holds for its reader.
    """
    config_name = metadata.get(MA_METADATA_KEY_NAME)
    names: list[str | None] = [name, config_name]
    if MA_METADATA_KEY_READER_OF in metadata:
        stamped = metadata.get(MA_METADATA_KEY_READER_SOURCE)
        if stamped:
            names.extend(stamped.split("\n"))
        else:
            names.extend(
                candidate[: -len(_READER_SUFFIX)]
                for candidate in (name, config_name)
                if candidate and candidate.endswith(_READER_SUFFIX)
            )
    return tuple(names)


def build_subject(
    *,
    is_admin: bool,
    platform_user_id: str | None,
    via_agent_key: bool = False,
    role_ids: tuple[str, ...] = (),
    verified_membership: bool | None = None,
    client: bool = False,
) -> Subject:
    """Who is acting. The one place a caller's identity becomes a `Subject`.

    A client is never an admin, whatever the caller passed.
    """
    return Subject(
        is_admin=is_admin and not client,
        platform_user_id=platform_user_id,
        via_agent_key=via_agent_key,
        client=client,
        role_ids=role_ids,
        verified_membership=verified_membership,
    )


def build_turn_place(
    *,
    channel_id: str,
    thread_id: str | None,
    category_id: str | None = None,
    category_unresolved: bool = False,
) -> Place:
    """Where a platform turn's reply lands: its thread, under its channel."""
    return Place(
        channel_id=thread_id or channel_id,
        parent_channel_id=channel_id,
        category_id=category_id,
        category_unresolved=category_unresolved,
    )


def build_agent_ref(
    name: str | None, metadata: Mapping[str, str], *extra_names: str | None
) -> AgentRef:
    """The agent an action is about, from its MA record (plus any cascade name)."""
    return AgentRef.of(*extra_names, *agent_names(name, metadata))


def _placed(policy: TenantAccessPolicy, at: Place) -> Place:
    return replace(
        at,
        permissions=channel_permissions(
            policy,
            channel_id=at.channel_id,
            parent_channel_id=at.parent_channel_id,
            category_id=at.category_id,
            category_unresolved=at.category_unresolved,
            parent_unresolved=at.parent_unresolved,
        ),
    )


def home_hold(policy: TenantAccessPolicy, agent: AgentRef, origin: Place | None) -> str | None:
    """The channel kept to its own agents that a call's reads and posts are held
    to (`daimon.core.permissions.held_to`); None when it is held nowhere. Pure.

    The rule `READ_CHANNEL` applies; a channel list asks it directly, since the
    name of a channel with limited readers may be listed where its messages
    may not be read.
    """
    if not agent.present:
        return None
    at = _placed(policy, origin).permissions if origin is not None else None
    return held_to(agent_permissions(policy, agent.names), at)


def authorize(
    policy: TenantAccessPolicy,
    *,
    subject: Subject,
    action: Action,
    surface: Surface = Surface.CHANNEL,
    agent: AgentRef | None = None,
    place: Place | None = None,
    recipient_id: str | None = None,
    origin_channel_ids: frozenset[str] = frozenset(),
    origin: Place | None = None,
    session: SessionFacts | None = None,
    operation_family: OperationFamily | None = None,
    reach: AgentReach | None = None,
    open_network: bool = False,
    answers_here: bool = False,
    recorded_seal_ids: frozenset[str] = frozenset(),
    approved: bool = False,
    fleet: FleetFacts | None = None,
    hold: TurnHold | None = None,
) -> Decision:
    """Decide one action against the tenant access policy. Pure; see the module docstring."""
    agent = agent if agent is not None else AgentRef.none()
    place = place if place is not None else Place()

    return _decide(
        policy,
        Request(
            subject=subject,
            action=action,
            surface=surface,
            agent=replace(agent, permissions=agent_permissions(policy, agent.names)),
            place=_placed(policy, place),
            recipient_id=recipient_id,
            origin_channel_ids=origin_channel_ids,
            origin=_placed(policy, origin) if origin is not None else None,
            session=session,
            operation_family=operation_family,
            reach=reach,
            open_network=open_network,
            answers_here=answers_here,
            recorded_seal_ids=recorded_seal_ids,
            approved=approved,
            fleet=fleet,
            hold=hold,
        ),
    )


__all__ = [
    "ALLOW",
    "Action",
    "FleetFacts",
    "AgentReach",
    "AgentRef",
    "Decision",
    "DenyReason",
    "OperationFamily",
    "Place",
    "Request",
    "SessionFacts",
    "Subject",
    "Surface",
    "agent_names",
    "authorize",
    "build_agent_ref",
    "build_subject",
    "build_turn_place",
    "holds_limited_readers",
    "home_hold",
]
