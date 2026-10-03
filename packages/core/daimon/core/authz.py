"""One decision for who may run, configure, post as, or read through an agent.

The channel rule, agent rule, invoker, admin-trust and fork rules are decided
here, in one pure function, `authorize` (what is still decided elsewhere is
listed under "Scope today" below). The turn pipeline, the MCP gates, the
channel tools, the routine and handoff tools and the fork paths describe the
action with the shared builders below (`build_subject`, `build_turn_place`,
`build_agent_ref`) and ask this module; each keeps only its own I/O and its
own refusal copy.

**Adding a rule.** Add the fact it needs as a field on `Subject`, `Place` or
`AgentRef` (with a default that keeps today's decisions), populate it in the
matching ``build_*`` function, and decide it in `_decide`. Admission records
the built values on the `Admission`, and every action-time re-check
(`daimon.core.turn.admission.reauthorize`, the MCP/hub turn re-check, the
send, form-submit and OAuth checks) asks `authorize` again with a fresh
policy. Two limits to check when adding a rule:

- `reauthorize` reuses the facts recorded at admission (only the policy is
  re-read). A rule whose facts can change during a turn needs those facts
  refreshed there too.
- Some callers still short-circuit before `authorize`: the hub's admin
  exemption in ``_ctx._policy_gate`` and `require_pin_write_access` for a
  trusted admin or a tenant without agent rules. A rule that must also bind
  admins or agents without a rule has to remove the matching short-circuit.
- Facts derived from the policy alone (`Place.permissions`,
  `AgentRef.permissions`, from `daimon.core.permissions`) are filled in by
  `authorize` itself, so every re-check sees them as the policy is now. Where
  an agent runs and posts, whom it messages, and whether it publishes, creates
  agents or is copied are that model's (`run_refusal`, `post_refusal`,
  `AgentPermissions`); this module adds who is asking.

The channel admin rules are the worked example: `Subject.administered_channel_ids`
is filled by `build_subject` (and `mcp_subject` on the MCP side) from stored
grants, and decided under CONFIGURE, MINT_CODING_TOKEN, SET_CHANNEL_ENVIRONMENT
and READ_SESSION. SET_CHANNEL_BUDGET and SET_CHANNEL_SKILLS are the
counter-examples: money and what a shared agent may do stay with server
admins, so a grant never counts there.

The rules, in the vocabulary of the formal model (`formal/access_control`):

- **Writers none** (`writers_none`): nothing writes into such a channel, a
  thread under one, or such a Discord category. No admin exemption.
- **Invoker allowlist** (`invoker_not_allowed`): when the tenant names who may
  start a turn, only they -- and admins -- may.
- **Agent rules** (`runs_elsewhere`): an agent whose rule names channels
  runs, posts and is configured only from inside them. A rule on ANY name
  the agent answers to applies (the cascade name, the MA name and the config
  name), and a rule naming no channel means "nowhere". A DM or a headless
  call is outside every rule.
- **Admin trust model**: admins are trusted, so they are exempt where the
  output reaches them alone -- their DM, their own hub turn -- and when they
  configure an agent. In a channel, thread, routine or handoff other members
  would see the result, so the agent rule holds for admins too. Who counts as
  an admin is the caller's trusted signal (`Subject.is_admin`): the live
  platform role for a platform turn, the stored role for a hub or form
  caller. Agent keys and tokens with no person behind them are never exempt.
- **Channel admins** hold a server admin's rights, limited to the channels
  they administer (`Subject.administered_channel_ids`, from stored grants;
  never `is_admin`). Over an agent they hold them only when it is theirs
  (`channel_admin_holds`): created by a channel admin for one of their
  channels, run only inside their channels by a server admin's agent rule,
  or made one of their channels' default by a server admin. The first and
  last also need the agent to stay local to their channels
  (`daimon.core.agent_reach`), as every admin-level change and default
  binding by them does. Only the first two let them bind it as their
  channel's default, since binding is what sets the default. Configuring an
  agent with a rule is theirs when they administer every channel its rules
  name; a rule naming no channel stays with server admins. So is minting a
  coding-tool token for such an agent, always bound to one of their channels
  inside its rule; an unbound token or an agent without a rule stays with
  server admins. On their own hub they read any conversation that ran in a
  channel they administer when every id that limited its readers lies there
  too (the channel, a thread under it, or a Slack ``channel:ts`` under it);
  never a private DM, an unstamped session, or to continue a limited one.
- **Sends under an agent rule**: wherever such an agent was admitted, it
  posts only into its channels (and threads under them) or the requester's
  own DM, and sends direct messages only to the requester.
- **Channel environments**: a server admin picks any channel's environment
  or the workspace default; a channel admin picks the channels they
  administer, except an environment with unrestricted networking in a
  channel whose readers are limited, or one holding such a thread
  (`not_a_reader`).
- **Channel and agent rules**: a server admin's call, never a channel
  admin's, even on a channel they administer.
- **Archiving a channel's copy**: a server admin's call; which copies may go
  is `daimon.core.channel_copies`'s.
- **Channel budgets**: only a server admin sets, clears or raises a channel's
  budget; a channel admin may not, even for the channels they administer.
- **Fork**: an admin's call, and an agent with a rule can't be copied at all.
- **Channel default**: nobody makes an agent the default of a channel outside
  its rule, since it would refuse every turn there. A channel admin
  (`Request.reach`) binds by the handoff rule below.
- **Handoff** (`HAND_OFF`): handing a thread to another agent is starting a
  turn there as that agent, so writers, the invoker allowlist and both rules
  apply, with no admin exemption. A member may bring in an agent scoped to
  the channel (the one it answers with, one whose rule names it, or one of
  its own agents); any other needs a server admin, or a channel admin of the
  parent channel when neither the place nor any live session in it had
  limited readers and they could bind the agent as its default
  (`Request.reach`: managed, tenant-wide, or theirs with `binding` and
  staying in their channels), unless the binding would take it out of
  another channel admin's channels while it is theirs.
- **Readers** (`not_a_reader`): a channel whose readers are inside, a thread
  under one, and a session that ran there are readable only from a turn
  inside every id that limited it.
- **Own agents** (`own_agents_only`): channel C whose readers are own keeps
  to its own agents, those whose rule names C alone
  (`AgentPermissions.home`). In C (`ChannelPermissions.home`) only they run,
  post, read and get routines or a default binding; a setup thread
  (`Place.setup_thread`) answers there as the built-in agent. They post
  nowhere outside C, not even the requester's DM, and send no direct
  messages or create agents, which would answer outside C; they publish
  only on the requester's approval (`Request.approved`). Admins are exempt as
  for agent rules: a server admin in their own DM or hub, and a channel admin
  of C there too. A call whose verified turn origin lies in C is held to C
  the same way, whatever agent runs it, and only C's own agents read C's
  sessions. Held to C, a call also reads only C and the sessions that ran in
  it (`home_hold`), so nothing from elsewhere is carried into C; an admin's
  hub read is exempt, since only they see it. A thread whose parent is
  unknown (`Place.parent_unresolved`) may lie in C, so it fails closed.

Nothing here does I/O: the callers load the policy and resolve the agent,
and decide again at the moment of action (`daimon.core.turn.admission.reauthorize`
before a session is built; a fresh policy read before every send, form
submit and OAuth grant).

Scope today: the agent rule decisions (turn admission, MCP and hub turns,
routine save and fire, handoff, configuration writes, form submits and the
OAuth callback), the caller gates of a platform turn and an MCP turn, sends
and direct messages under an agent rule, channel and session reads
(including the hub's admin and channel admin reads and the refusal to
continue a limited conversation), fork, channel default binds, channel
environment picks, channel and agent rule changes, channel copy archives,
coding-tool token mints, agent creation (`CREATE_AGENT`), publishing
(`PUBLISH`), the writers rule on a turn's own notices and on a routine's
destination (`POST` with no agent), a routine's creator at fire and delivery
(`ACT_FOR_CREATOR`), and the shared-agent table (`CHANGE_SHARED_AGENT`, asked by
`daimon.core.operation_policy`).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Literal

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


class Action(StrEnum):
    """What the subject wants to do."""

    # A platform turn's caller gates: the reply target's writers, then the
    # invoker allowlist. Runs before the agent is known.
    START_TURN = "start_turn"
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
    # may still start a turn (the invoker allowlist; admins always may). A
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
    # Hand a thread's conversation to another agent (`Place` is the thread).
    # Writers and the invoker allowlist as START_TURN, then RUN_AGENT on the
    # handoff surface, then who may bring that agent here: anyone when it is
    # scoped to this channel (`Request.answers_here`, a rule naming it, or one
    # of the channel's own agents); otherwise a server admin, or a channel
    # admin of the parent channel unless the place's readers are limited.
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
    CONFIG = "config"  # a configuration write (request tool, form, OAuth, direct tool)


#: Surfaces whose output reaches only the acting admin.
_ADMIN_ONLY_SURFACES = frozenset({Surface.DM, Surface.HUB})


@dataclass(frozen=True)
class Subject:
    """Who is acting.

    ``is_admin`` is the caller's trusted admin signal for this surface (see the
    module docstring); a caller that never trusts an admin -- an agent key, a
    bearer token -- passes False. ``platform_user_id`` is None for a token with
    no person behind it. ``administered_channel_ids`` are the channels a stored
    channel admin grant gives the subject; a server admin needs none.
    """

    is_admin: bool = False
    platform_user_id: str | None = None
    # The caller holds an agent-scoped key: never exempt as an admin, whoever
    # minted it, because the key is long-lived and its role may be stale.
    via_agent_key: bool = False
    administered_channel_ids: frozenset[str] = frozenset()


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
    """How far a change to an agent, or a channel admin's handoff to it, reaches.

    ``managed`` is a defaults-managed agent; ``reachable`` one that answers
    for others in the tenant (a channel or workspace default);
    ``local_to_caller`` one whose every place lies in the caller's
    administered channels (`daimon.core.agent_reach`); and ``held_by_caller``
    one that is the caller's as a channel admin (`channel_admin_holds`).

    For HAND_OFF and BIND_CHANNEL_DEFAULT (`load_binding_reach`):
    ``local_to_caller`` also counts an agent that answers nowhere yet,
    ``held_by_caller`` is read with `binding`, ``tenant_wide`` is the tenant
    default or the deployment fall-through, and ``held_by_other_admin`` an
    agent another channel admin holds and keeps local to channels without
    this place, which a binding here would take away.
    """

    managed: bool = False
    reachable: bool = False
    local_to_caller: bool = False
    held_by_caller: bool = False
    tenant_wide: bool = False
    held_by_other_admin: bool = False


@dataclass(frozen=True)
class AgentStanding:
    """How an agent came to a channel admin's channels (`channel_admin_holds`).

    ``created_for_channel_id`` is the channel a channel admin of it created the
    agent for, from there; ``admin_default_channel_ids`` the channels whose
    default a server admin set to it.
    """

    created_for_channel_id: str | None = None
    admin_default_channel_ids: frozenset[str] = frozenset()


DenyReason = Literal[
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


def _outside_rule(agent: AgentRef, place: Place) -> bool:
    return outside_runs_in(agent.permissions, place.channel_id, place.parent_channel_id)


def _rule_administered(
    policy: TenantAccessPolicy, agent: AgentRef, administered_channel_ids: frozenset[str]
) -> bool:
    """Whether every channel the rules on any of the agent's names name is administered.

    False for an agent without a rule and for a rule naming no channel, which is nobody's.
    """
    return agent_permissions(policy, agent.names).runs_within(administered_channel_ids)


def _origin_permissions(req: Request) -> ChannelPermissions | None:
    return req.origin.permissions if req.origin is not None else None


def _held_to(agent: AgentRef, origin: Place | None) -> str | None:
    return held_to(agent.permissions, origin.permissions if origin is not None else None)


def _home_admin(subject: Subject, agent: AgentRef) -> bool:
    """A channel admin of the agent's home channel, exempt as a server admin is."""
    own = agent.permissions.home
    return (
        own is not None
        and subject.platform_user_id is not None
        and not subject.via_agent_key
        and own in subject.administered_channel_ids
    )


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
    subject, place = req.subject, req.place
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
    parent = place.parent_channel_id or place.channel_id
    if (
        parent is not None
        and subject.platform_user_id is not None
        and not subject.via_agent_key
        and parent in subject.administered_channel_ids
    ):
        # Content with limited readers would reach an agent with its own keys
        # and connectors: a server admin's call, as for an open network. A
        # session limited before the limit was lifted still holds that content.
        if place.permissions.readers != "any" or req.recorded_seal_ids:
            return _deny("not_a_reader")
        # The default-binding rule: answering here would lend another
        # channel's own agent's keys and memory to this channel.
        if _channel_admin_binds(req.reach or AgentReach()):
            return ALLOW
    return _deny("admin_required")


def _channel_admin_binds(reach: AgentReach) -> bool:
    """A channel admin's default binding or handoff: an agent shared by design,
    or theirs and staying in their channels, unless another channel admin
    would lose their hold on it."""
    bindable = reach.managed or reach.tenant_wide
    bindable = bindable or (reach.local_to_caller and reach.held_by_caller)
    return bindable and not reach.held_by_other_admin


def _writers_none(place: Place) -> bool:
    return place.permissions.writers == "none"


def _seal_id_administered(
    seal_id: str, facts: SessionFacts, administered_channel_ids: frozenset[str]
) -> bool:
    """An administered channel, the session's own thread under one, or a Slack
    thread (``channel:ts``) under one. A thread limited elsewhere has no known parent."""
    if seal_id in administered_channel_ids:
        return True
    if seal_id == facts.thread and facts.channel in administered_channel_ids:
        return True
    channel, separator, _ = seal_id.partition(":")
    return bool(separator) and channel in administered_channel_ids


def _channel_admin_hub_read(subject: Subject, facts: SessionFacts) -> bool:
    """A channel admin's hub read: anyone's conversation that ran in a channel they
    administer, limited only by ids that lie there too. Never a private DM or an
    unstamped session, which can't be placed."""
    administered = subject.administered_channel_ids
    return (
        subject.platform_user_id is not None
        and not subject.via_agent_key
        and not facts.private
        and facts.channel is not None
        and facts.channel in administered
        and all(_seal_id_administered(seal_id, facts, administered) for seal_id in facts.seal_ids)
    )


def _decide_shared_agent_change(req: Request) -> Decision:
    """The blast-radius table (`daimon.core.operation_policy`): a change reaching one
    agent is any member's, one reaching the tenant is an admin's.

    A posted-token write always passes. A spec edit refuses a managed agent even
    for an admin, then passes an admin; an attachment write passes an admin
    before the managed check. Then a reachable agent needs an admin unless it is
    local to the caller's administered channels and theirs (`channel_admin_holds`).
    """
    reach = req.reach or AgentReach()
    if req.operation_family == "posted_token":
        return ALLOW
    if req.operation_family == "spec":
        if reach.managed:
            return _deny("managed_agent")
        if req.subject.is_admin:
            return ALLOW
    else:
        if req.subject.is_admin:
            return ALLOW
        if reach.managed:
            return _deny("managed_agent")
    if reach.reachable and not (reach.local_to_caller and reach.held_by_caller):
        return _deny("admin_required")
    return ALLOW


def channel_admin_holds(
    policy: TenantAccessPolicy,
    *,
    subject: Subject,
    agent: AgentRef,
    standing: AgentStanding,
    binding: bool = False,
) -> bool:
    """Whether an agent is the subject's to administer as a channel admin.

    It is when a channel admin created it for one of their channels, a server
    admin's rule runs it inside their channels (`_rule_administered`), or, except
    when `binding` it as a default, a server admin made it one of their
    channels' default. Locality is the caller's to add
    (`daimon.core.agent_reach`). Never for an agent key or a token with no
    person behind it.
    """
    administered = subject.administered_channel_ids
    if subject.via_agent_key or subject.platform_user_id is None:
        return False
    return agent_held_in(
        policy, agent=agent, standing=standing, channel_ids=administered, binding=binding
    )


def agent_held_in(
    policy: TenantAccessPolicy,
    *,
    agent: AgentRef,
    standing: AgentStanding,
    channel_ids: frozenset[str],
    binding: bool = False,
) -> bool:
    """`channel_admin_holds` for a channel admin of `channel_ids`, whoever they are."""
    if not channel_ids:
        return False
    if standing.created_for_channel_id in channel_ids:
        return True
    if _rule_administered(policy, agent, channel_ids):
        return True
    return not binding and not standing.admin_default_channel_ids.isdisjoint(channel_ids)


def _decide(policy: TenantAccessPolicy, req: Request) -> Decision:
    subject, agent, place = req.subject, req.agent, req.place
    admin_only_surface = req.surface in _ADMIN_ONLY_SURFACES

    if req.action is Action.START_TURN:
        if _writers_none(place):
            return _deny("writers_none")
        if subject.platform_user_id is not None and not is_invoker_allowed(
            policy, external_user_id=subject.platform_user_id, is_admin=subject.is_admin
        ):
            return _deny("invoker_not_allowed")
        return ALLOW

    if req.action is Action.ACT_FOR_CREATOR:
        if subject.platform_user_id is None or not is_invoker_allowed(
            policy, external_user_id=subject.platform_user_id, is_admin=subject.is_admin
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
        if admin_only_surface and _home_admin(subject, agent):
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
        if not subject.via_agent_key and _rule_administered(
            policy, agent, subject.administered_channel_ids
        ):
            return ALLOW
        if _outside_rule(agent, place):
            return _deny("runs_elsewhere")
        return ALLOW

    if req.action is Action.BIND_CHANNEL_DEFAULT:
        if _outside_rule(agent, place):
            return _deny("runs_elsewhere")
        if agent.present and crosses_home(agent.permissions, place.permissions):
            return _deny("own_agents_only")
        # A channel admin's binding carries `reach`; the handoff rule decides it.
        if req.reach is not None and not _channel_admin_binds(req.reach):
            return _deny("admin_required")
        return ALLOW

    if req.action is Action.SET_CHANNEL_ENVIRONMENT:
        if subject.is_admin and not subject.via_agent_key:
            return ALLOW
        channel = place.parent_channel_id or place.channel_id
        if subject.via_agent_key or channel not in subject.administered_channel_ids:
            return _deny("admin_required")
        # Content with limited readers must not leave through an open network
        # that the channel admin chose; that is a server admin's call. The
        # environment covers every thread under the channel, so a limited one counts.
        if req.open_network and holds_limited_readers(policy, channel, place, req.origin):
            return _deny("not_a_reader")
        return ALLOW

    if req.action in (
        Action.ARCHIVE_CHANNEL_COPY,
        Action.SET_CHANNEL_RULE,
        Action.SET_AGENT_RULE,
    ):
        # The rules guard what a server admin owns, so a channel admin changes
        # none, even on their own channel.
        if subject.is_admin and not subject.via_agent_key:
            return ALLOW
        return _deny("admin_required")

    if req.action in (Action.SET_CHANNEL_BUDGET, Action.SET_CHANNEL_SKILLS):
        # Money, and what a shared agent may do, stay with server admins; an
        # admin's own agent key keeps the rights it always had.
        return ALLOW if subject.is_admin else _deny("admin_required")

    if req.action is Action.MINT_CODING_TOKEN:
        if subject.is_admin and not subject.via_agent_key:
            return ALLOW
        # A channel admin's token is always bound, to a channel they administer
        # inside a rule they administer wholly; an agent without one answers anywhere.
        administered = subject.administered_channel_ids
        if subject.via_agent_key or place.channel_id not in administered:
            return _deny("admin_required")
        if not agent.resolved:
            return _deny("agent_unresolved")
        if not agent.permissions.runs_in:
            return _deny("admin_required")
        if not _rule_administered(policy, agent, administered) or _outside_rule(agent, place):
            return _deny("runs_elsewhere")
        return ALLOW

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
    # A channel admin holds the same read for their channels.
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
    if hub_read and (admin_hub_read or _channel_admin_hub_read(subject, facts)):
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
    administered_channel_ids: Collection[str] = (),
) -> Subject:
    """Who is acting. The one place a caller's identity becomes a `Subject`.

    ``administered_channel_ids`` are the channels the caller's stored channel
    admin grants name; an agent key or a token with no person behind it holds
    none, whatever its account's grants.
    """
    holds_grants = platform_user_id is not None and not via_agent_key
    return Subject(
        is_admin=is_admin,
        platform_user_id=platform_user_id,
        via_agent_key=via_agent_key,
        administered_channel_ids=frozenset(administered_channel_ids)
        if holds_grants
        else frozenset(),
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
        ),
    )


__all__ = [
    "ALLOW",
    "Action",
    "AgentReach",
    "AgentRef",
    "AgentStanding",
    "Decision",
    "DenyReason",
    "OperationFamily",
    "Place",
    "Request",
    "SessionFacts",
    "Subject",
    "Surface",
    "agent_held_in",
    "agent_names",
    "authorize",
    "build_agent_ref",
    "build_subject",
    "build_turn_place",
    "channel_admin_holds",
    "holds_limited_readers",
    "home_hold",
]
