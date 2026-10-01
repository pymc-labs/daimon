"""One decision for who may run, configure, post as, or read through an agent.

Every permission rule that depends on the tenant access policy lives here, in
one pure function, `authorize`. The turn pipeline, the MCP gates, the channel
tools, the routine and handoff tools and the fork paths gather the facts they
already hold (who is acting, which agent, where, doing what) and ask this
module; each keeps only its own I/O and its own refusal copy.

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

Nothing here does I/O; `daimon.core.authz_context` and the callers load the
policy and resolve the agent.
"""

from __future__ import annotations

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


class Action(StrEnum):
    """What the subject wants to do."""

    # A platform turn's caller gates: protection of the reply target, then the
    # invoker allowlist. Runs before the agent is known.
    START_TURN = "start_turn"
    # Run a resolved agent here (a turn, a routine fire, a handoff binding).
    RUN_AGENT = "run_agent"
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

    ``channel`` / ``thread`` are the stamps it was created with,
    ``seal_ids`` every id that sealed it, and ``legacy_thread_id`` the thread
    an unstamped (pre-stamp) session ran on, if any.
    """

    channel: str | None = None
    thread: str | None = None
    seal_ids: frozenset[str] = frozenset()
    legacy_thread_id: str | None = None


DenyReason = Literal[
    "channel_protected",
    "invoker_not_allowed",
    "agent_pinned_elsewhere",
    "agent_unresolved",
    "dm_recipient_not_requester",
    "admin_required",
    "agent_pinned",
    "sealed",
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
        if subject.is_admin and admin_only_surface and subject.platform_user_id is not None:
            return ALLOW
        if not agent.present:
            return ALLOW
        if not agent.resolved:
            return _deny("agent_unresolved") if policy.agent_channel_pins else ALLOW
        if _outside_pin(policy, agent, place):
            return _deny("agent_pinned_elsewhere")
        return ALLOW

    if req.action is Action.CONFIGURE:
        if subject.is_admin or not policy.agent_channel_pins:
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
        if not subject.is_admin:
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

    # Action.READ_SESSION
    facts = req.session or SessionFacts()
    if _session_readable(policy, req.origin_channel_ids, facts):
        return ALLOW
    return _deny("sealed")


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
    "authorize",
    "channel_readable",
]
