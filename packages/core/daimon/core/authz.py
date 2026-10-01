"""One decision for who may run, configure, post as, or read through an agent.

The pin, seal, protection, invoker, admin-trust and fork rules are decided
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
(`daimon.core.turn.admission.reauthorize`, the send, form-submit and OAuth
checks) asks `authorize` with values from the same builders, so a new rule is
applied at action time too without touching those call sites.

**Example: channel admins reading their channels' sealed sessions.** Add
``administered_channel_ids: frozenset[str] = frozenset()`` to `Subject`,
populate it in `build_subject` (and `mcp_subject` on the MCP side), and in
`_decide` allow `Action.READ_SESSION` when ``facts.seal_ids`` is non-empty,
every seal id is in ``subject.administered_channel_ids`` and the session is
not ``facts.private``. Never for `Action.CONTINUE_SESSION`. The hub's session
reads already ask `authorize(READ_SESSION)` with the session's seal ids, so
nothing else changes.

The rules, in the vocabulary of the formal model (`formal/access_control`):

- **Protection** (`channel_protected`): nothing writes into a protected
  channel, a thread under one, or a protected Discord category. No admin
  exemption.
- **Invoker allowlist** (`invoker_not_allowed`): when the tenant names who may
  start a turn, only they -- and admins -- may.
- **Pins** (`agent_pinned_elsewhere`): an agent pinned to channels runs, posts
  and is configured only from inside them. A pin on ANY name the agent answers
  to applies (the cascade name, the MA name and the config name), and an empty
  pin list means "nowhere". A DM or a headless call is outside every pin.
- **Admin trust model**: admins are trusted, so they are exempt where the
  output reaches them alone -- their DM, their own hub turn -- and when they
  configure an agent. In a channel, thread, routine or handoff other members
  would see the result, so the pin holds for admins too. Who counts as an
  admin is the caller's trusted signal (`Subject.is_admin`): the live platform
  role for a platform turn, the stored role for a hub or form caller. Agent
  keys and tokens with no person behind them are never exempt.
- **Pinned sends**: wherever a pinned agent was admitted, it posts only into
  its pinned channels (and threads under them) or the requester's own DM,
  and sends direct messages only to the requester.
- **Fork**: an admin's call, and a pinned agent can't be copied at all.
- **Seals**: a sealed channel, a thread under one, and a session that ran
  under a seal are readable only from a turn inside every id that sealed it.

Nothing here does I/O: the callers load the policy and resolve the agent,
and decide again at the moment of action (`daimon.core.turn.admission.reauthorize`
before a session is built; a fresh policy read before every send, form
submit and OAuth grant).

Scope today: the pin decisions (turn admission, MCP and hub turns, routine
save and fire, handoff, configuration writes, form submits and the OAuth
callback), the caller gates of a platform turn and an MCP turn, pinned sends
and direct messages, channel and session reads (including the hub's admin
read and the refusal to continue a sealed conversation), and fork. Still
decided outside this module, by the same `daimon.core.access_policy`
predicates: the live protection and invoker checks in the scheduler, routine
save and delivery and turn-reply protection; and the shared-agent
replace/remove table (`daimon.core.operation_policy`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from daimon.core.access_policy import (
    DM_SCOPE_PREFIX,
    TenantAccessPolicy,
    is_invoker_allowed,
    is_outside_agent_pin,
    is_write_protected,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_READER_OF,
    MA_METADATA_KEY_READER_SOURCE,
)


class Action(StrEnum):
    """What the subject wants to do."""

    # A platform turn's caller gates: protection of the reply target, then the
    # invoker allowlist. Runs before the agent is known.
    START_TURN = "start_turn"
    # Run a resolved agent here (a turn, a routine fire, a handoff binding).
    RUN_AGENT = "run_agent"
    # Save (create or update) a routine for an agent: RUN_AGENT at the
    # routine's destination, but only once a non-empty name of the agent is
    # pinned (an empty name never makes a routine pinned).
    SAVE_ROUTINE = "save_routine"
    # Change what an agent reaches: keys, connectors, MCP servers, prompt,
    # tools, skills, repo binding.
    CONFIGURE = "configure"
    # Post into a channel, thread or conversation as the executing agent.
    POST = "post"
    # Send a direct message to a workspace member as the executing agent.
    DIRECT_MESSAGE = "direct_message"
    # Copy an agent.
    FORK = "fork"
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
    no person behind it.
    """

    is_admin: bool = False
    platform_user_id: str | None = None
    # The caller holds an agent-scoped key: never exempt as an admin, whoever
    # minted it, because the key is long-lived and its role may be stale.
    via_agent_key: bool = False


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

    @classmethod
    def from_origin(cls, *, parent_channel_id: str | None, thread_id: str | None) -> Place:
        """The place a recorded turn origin stands for.

        A private DM conversation's scope id (``dm:…``) stands in for its
        thread id; a DM runs in no workspace channel, so it is outside every
        pin, as ``admit(is_dm=True)`` treats it.
        """
        if thread_id is not None and thread_id.startswith(DM_SCOPE_PREFIX):
            return cls()
        return cls(channel_id=thread_id or parent_channel_id, parent_channel_id=parent_channel_id)


@dataclass(frozen=True)
class SessionFacts:
    """What a recorded session says about where it ran (READ_SESSION only).

    ``owned`` is whether the session belongs to the caller's own account;
    ``private`` marks a DM conversation (the private-DM stamp, a ``dm:``
    scope, a Slack IM or a Teams personal chat), which no one else ever reads.
    ``channel`` / ``thread`` are the stamps it was created with,
    ``seal_ids`` every id that sealed it, and ``legacy_thread_id`` the thread
    an unstamped (pre-stamp) session ran on, if any.
    """

    channel: str | None = None
    thread: str | None = None
    seal_ids: frozenset[str] = frozenset()
    legacy_thread_id: str | None = None
    owned: bool = True
    private: bool = False


DenyReason = Literal[
    "channel_protected",
    "invoker_not_allowed",
    "agent_pinned_elsewhere",
    "agent_unresolved",
    "dm_recipient_not_requester",
    "admin_required",
    "agent_pinned",
    "sealed",
    "not_owner",
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
    session: SessionFacts | None = None


def _names_pinned(policy: TenantAccessPolicy, names: tuple[str | None, ...]) -> bool:
    return any(name is not None and name in policy.agent_channel_pins for name in names)


def _outside_pin(policy: TenantAccessPolicy, agent: AgentRef, place: Place) -> bool:
    return is_outside_agent_pin(
        policy,
        agent_names=agent.names,
        channel_id=place.channel_id,
        parent_channel_id=place.parent_channel_id,
    )


def _protected(policy: TenantAccessPolicy, place: Place) -> bool:
    if place.channel_id is None:
        return False
    return is_write_protected(
        policy,
        channel_id=place.channel_id,
        parent_channel_id=place.parent_channel_id,
        category_id=place.category_id,
        category_unresolved=place.category_unresolved,
    )


def channel_readable(
    policy: TenantAccessPolicy,
    origin_channel_ids: frozenset[str],
    channel_id: str,
    parent_channel_id: str | None = None,
) -> bool:
    """Whether a sealed channel (or a thread under one) is readable from the calling turn."""
    sealed = policy.sealed_channel_ids
    if channel_id in sealed:
        return channel_id in origin_channel_ids
    if parent_channel_id is not None and parent_channel_id in sealed:
        return parent_channel_id in origin_channel_ids
    return True


def _session_readable(
    policy: TenantAccessPolicy, origin_channel_ids: frozenset[str], facts: SessionFacts
) -> bool:
    """The seal rule for a recorded session's transcript.

    A stamped session is judged like a channel read of the channel and thread
    it ran in, against the current policy, and stays inside every id that
    sealed it after an unseal. An unstamped session a thread ran on is shown
    only inside that thread while the tenant seals anything; one no thread ran
    on is headless and carries no channel content.
    """
    if facts.channel is not None:
        if not facts.seal_ids <= origin_channel_ids:
            return False
        if facts.thread is None:
            return channel_readable(policy, origin_channel_ids, facts.channel)
        # A Slack thread is sealed on its own as channel_id:thread_ts.
        return channel_readable(
            policy, origin_channel_ids, facts.thread, facts.channel
        ) and channel_readable(
            policy, origin_channel_ids, f"{facts.channel}:{facts.thread}", facts.channel
        )
    if facts.legacy_thread_id is None or not policy.sealed_channel_ids:
        return True
    return facts.legacy_thread_id in origin_channel_ids


def _decide(policy: TenantAccessPolicy, req: Request) -> Decision:
    subject, agent, place = req.subject, req.agent, req.place
    admin_only_surface = req.surface in _ADMIN_ONLY_SURFACES

    if req.action is Action.START_TURN:
        if _protected(policy, place):
            return _deny("channel_protected")
        if subject.platform_user_id is not None and not is_invoker_allowed(
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
        if not agent.present:
            return ALLOW
        if not agent.resolved:
            return _deny("agent_unresolved") if policy.agent_channel_pins else ALLOW
        if _outside_pin(policy, agent, place):
            return _deny("agent_pinned_elsewhere")
        return ALLOW

    if req.action is Action.SAVE_ROUTINE:
        if not any(name in policy.agent_channel_pins for name in agent.names if name):
            return ALLOW
        if _outside_pin(policy, agent, place):
            return _deny("agent_pinned_elsewhere")
        return ALLOW

    if req.action is Action.CONFIGURE:
        if (subject.is_admin and not subject.via_agent_key) or not policy.agent_channel_pins:
            return ALLOW
        if not agent.resolved:
            return _deny("agent_unresolved")
        if _outside_pin(policy, agent, place):
            return _deny("agent_pinned_elsewhere")
        return ALLOW

    if req.action is Action.POST:
        if _protected(policy, place):
            return _deny("channel_protected")
        if place.own_dm or not policy.agent_channel_pins or not agent.present:
            return ALLOW
        if not agent.resolved:
            return _deny("agent_unresolved")
        if _outside_pin(policy, agent, place):
            return _deny("agent_pinned_elsewhere")
        return ALLOW

    if req.action is Action.DIRECT_MESSAGE:
        if not agent.present or not policy.agent_channel_pins:
            return ALLOW
        if not agent.resolved:
            return _deny("agent_unresolved")
        if not _names_pinned(policy, agent.names):
            return ALLOW
        if req.recipient_id != subject.platform_user_id:
            return _deny("dm_recipient_not_requester")
        return ALLOW

    if req.action is Action.FORK:
        if not subject.is_admin or subject.via_agent_key:
            return _deny("admin_required")
        if _names_pinned(policy, agent.names):
            return _deny("agent_pinned")
        return ALLOW

    if req.action is Action.READ_CHANNEL:
        if place.channel_id is None:
            return ALLOW
        if channel_readable(
            policy, req.origin_channel_ids, place.channel_id, place.parent_channel_id
        ):
            return ALLOW
        return _deny("sealed")

    # Action.READ_SESSION / Action.CONTINUE_SESSION
    facts = req.session or SessionFacts()
    # Admins are trusted to READ any channel conversation of the agent from
    # their own hub session, sealed or not, anyone's -- never a private DM,
    # and never to continue it (a follow-up would join the channel's session).
    admin_hub_read = (
        req.action is Action.READ_SESSION
        and req.surface is Surface.HUB
        and subject.is_admin
        and subject.platform_user_id is not None
        and not subject.via_agent_key
        and not facts.private
        and (facts.channel is not None or facts.legacy_thread_id is not None)
    )
    if admin_hub_read:
        return ALLOW
    if not facts.owned:
        return _deny("not_owner")
    if _session_readable(policy, req.origin_channel_ids, facts):
        return ALLOW
    return _deny("sealed")


_READER_SUFFIX = "-reader"


def agent_names(name: str | None, metadata: Mapping[str, str]) -> tuple[str | None, ...]:
    """Every name a pin on an agent may be keyed by: its MA name and its config name.

    A published report's reader variant answers as its source agent, so it
    also carries the source's names (stamped as `daimon_reader_source`; a
    reader written before that stamp falls back to its name without the
    ``-reader`` suffix): a pin on the source holds for its reader.
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
    *, is_admin: bool, platform_user_id: str | None, via_agent_key: bool = False
) -> Subject:
    """Who is acting. The one place a caller's identity becomes a `Subject`."""
    return Subject(
        is_admin=is_admin, platform_user_id=platform_user_id, via_agent_key=via_agent_key
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
    session: SessionFacts | None = None,
) -> Decision:
    """Decide one action against the tenant access policy. Pure; see the module docstring."""
    return _decide(
        policy,
        Request(
            subject=subject,
            action=action,
            surface=surface,
            agent=agent if agent is not None else AgentRef.none(),
            place=place if place is not None else Place(),
            recipient_id=recipient_id,
            origin_channel_ids=origin_channel_ids,
            session=session,
        ),
    )


__all__ = [
    "ALLOW",
    "Action",
    "AgentRef",
    "Decision",
    "DenyReason",
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
    "channel_readable",
]
