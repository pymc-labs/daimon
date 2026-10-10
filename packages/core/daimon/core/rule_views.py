"""What the channel and agent rules show and refuse at each place.

The rules are defined in `daimon.core.permissions` and decided by
`daimon.core.authz.authorize`. This module holds what the agent and setup
surfaces show of them: who is listed where (`RuleViewer`), routines kept
inside, default bindings. Members see only the responders and explicit local
rules at their location,
even when no channel limits its readers.

Everything here is pure but `load_rule_viewer` and `is_thread_turn_refused`;
`daimon.core.channel_rules` sets the rules.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_pins import agent_aliases, agent_pin_names
from daimon.core.authz import Action, AgentRef, Place, Subject, authorize, build_turn_place
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from daimon.core.defaults.metadata import private_routine_stamp
from daimon.core.errors import DaimonError
from daimon.core.permissions import (
    agent_permissions,
    any_agent_rules,
    any_own_readers,
    channel_permissions,
    channel_rule,
    home_of,
    limiting_ids_at,
    listed_at,
    runs_at,
)
from daimon.core.routine_delivery import delivery_target, teams_channel_of
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.scoped_config_read import resolve
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BindingRefusal = Literal["agent_has_home", "agent_runs_elsewhere", "not_own_agent", "keeps_own"]
"""Why a routing write would break a rule.

`agent_has_home`: the agent is another channel's own agent. `agent_runs_elsewhere`:
its rule names other channels, so admission would refuse every turn there.
`not_own_agent`: only the channel's own agents run there, and it is not one.
`keeps_own`: clearing such a channel's default would hand it to a shared agent.
"""


def render_binding_refusal(reason: BindingRefusal, *, agent_name: str | None) -> str:
    """The person-facing reason for a refused routing write."""
    name = agent_name or "That agent"
    match reason:
        case "agent_has_home":
            return f"{name} is another channel's own agent, so it can't be used here."
        case "agent_runs_elsewhere":
            return (
                f"{name}'s agent rule names other channels, so it would refuse every turn here. "
                "Pick another agent, or change its rule first."
            )
        case "not_own_agent":
            return (
                f"Only this channel's own agents answer here, and {name} is not one of them. "
                "Use a copy of it, or give it an agent rule naming this channel alone."
            )
        case "keeps_own":
            return (
                "Only this channel's own agents answer here, so its default stays one of them. "
                "Set another of its own agents, or change who can read the channel first."
            )


def binding_refusal(
    policy: TenantAccessPolicy,
    *,
    agent_names: tuple[str | None, ...],
    channel_id: str | None,
    parent_channel_id: str | None = None,
) -> BindingRefusal | None:
    """Why routing the agent at `channel_id` (None: the tenant default) is refused.

    `authorize(BIND_CHANNEL_DEFAULT)` decides, for everyone: an agent routed
    outside its rule would refuse every turn there. Pass every name the agent
    carries, and a thread binding's parent channel.
    """
    decision = authorize(
        policy,
        subject=Subject(),
        action=Action.BIND_CHANNEL_DEFAULT,
        agent=AgentRef.of(*agent_names),
        place=Place(channel_id=channel_id, parent_channel_id=parent_channel_id),
    )
    if decision.reason == "runs_elsewhere":
        home = agent_permissions(policy, agent_names).home
        return "agent_has_home" if home else "agent_runs_elsewhere"
    if decision.reason == "own_agents_only":
        return "not_own_agent"
    return None


def clear_refusal(policy: TenantAccessPolicy, *, channel_id: str) -> BindingRefusal | None:
    """A channel only its own agents read keeps one of them as its default."""
    return "keeps_own" if channel_rule(policy, channel_id).readers == "own" else None


def routine_destination_channel(row: RoutineRow) -> str | None:
    """The channel a routine delivers into (a thread's parent), or None without a destination.

    The saved `channel_id` is the destination's parent; a row saved before it
    was recorded falls back to the channel its destination id carries, or the
    id itself (a channel, or a Discord thread whose parent is unknown).
    """
    if row.destination_kind is None or row.destination_id is None:
        return None
    if row.channel_id:
        return row.channel_id
    if row.destination_kind == "channel":
        return row.destination_id
    return _channel_in_thread_id(row.destination_id) or row.destination_id


def is_routine_parent_unknown(row: RoutineRow) -> bool:
    """A thread destination saved before its parent channel was recorded, whose id
    doesn't carry it (a Discord thread; a Slack one is ``channel:ts``, a Teams one
    ``19:…;messageid=…``)."""
    return (
        row.destination_kind == "thread"
        and row.channel_id is None
        and row.destination_id is not None
        and _channel_in_thread_id(row.destination_id) is None
    )


def _channel_in_thread_id(thread_id: str) -> str | None:
    """The channel a thread id carries: Teams and Slack ids do, a Discord id doesn't."""
    teams = teams_channel_of(thread_id)
    if teams is not None:
        return teams
    channel, sep, _ = thread_id.partition(":")
    return channel if sep and channel else None


def routine_destination_place(row: RoutineRow, *, channel_id: str | None) -> Place:
    """Where a routine fires into: `channel_id` under its saved parent channel.

    A thread whose parent is unknown (`is_routine_parent_unknown`) is marked
    `parent_unresolved`, so `authorize` refuses it while any channel limits its readers.
    """
    return Place(
        channel_id=channel_id,
        parent_channel_id=routine_destination_channel(row),
        parent_unresolved=is_routine_parent_unknown(row),
    )


@dataclass(frozen=True)
class RoutineOrigin:
    """Where a routine's session runs, stamped on it like a turn there, and
    the private stamp that keeps its transcript its owner's alone."""

    channel_id: str
    thread_id: str | None
    seal_ids: frozenset[str]
    private_dm_id: str


def routine_origin(
    policy: TenantAccessPolicy, row: RoutineRow, *, platform: str
) -> RoutineOrigin | None:
    """The channel and thread a routine fires into, with the readers limits over them now.

    Stamped on the routine's session so the routine transcript of a channel
    with limited readers stays inside it. A routine without a destination is
    placed by its saved channel (for one made in a DM, the channel the DM
    came from); one with neither is headless (None). Every stamp is private
    too (`private_routine_stamp`): the routine runs on its owner's
    credentials, so no admin or channel admin reads it from the hub, as
    before it carried a channel.
    """
    channel_id = routine_destination_channel(row) or row.channel_id
    if channel_id is None:
        return None
    thread_id: str | None = None
    if row.destination_kind == "thread" and row.destination_id is not None:
        # Slack names a thread by its ts under the channel; Discord and Teams by its own id.
        target = delivery_target(row, platform=platform)
        thread_id = (
            (target.thread_ts if target is not None else None)
            if platform == "slack"
            else row.destination_id
        )
    if thread_id == channel_id:
        thread_id = None
    return RoutineOrigin(
        channel_id=channel_id,
        thread_id=thread_id,
        seal_ids=limiting_ids_at(policy, channel_id=channel_id, thread_id=thread_id),
        private_dm_id=private_routine_stamp(row.id),
    )


def keeps_routine_inside(
    policy: TenantAccessPolicy, row: RoutineRow, *, parent_channel_id: str | None = None
) -> bool:
    """Whether a routine's result must stay in a channel only its own agents read, so
    never goes by DM.

    The destination decides, so the agent needn't be resolved: such a
    channel's own agent fires only into that channel, as its rule refuses every
    other destination, by every name, at save and at each fire. Pass
    `parent_channel_id` once the destination's parent is resolved; until then a
    thread whose parent is unknown stays inside while any channel is kept to its own agents.
    """
    if channel_permissions(policy, channel_id=parent_channel_id).keeps_content:
        return True
    return channel_permissions(
        policy,
        channel_id=routine_destination_channel(row),
        parent_unresolved=parent_channel_id is None and is_routine_parent_unknown(row),
    ).keeps_content


def is_memory_hidden(
    policy: TenantAccessPolicy,
    *,
    agent_names: tuple[str | None, ...],
    channel_id: str,
    parent_channel_id: str | None = None,
) -> bool:
    """Whether an agent's memory stays hidden at a place: wherever it may not run."""
    return not authorize(
        policy,
        subject=Subject(),
        action=Action.RUN_AGENT,
        agent=AgentRef.of(*agent_names),
        place=Place(channel_id=channel_id, parent_channel_id=parent_channel_id),
    )


@dataclass(frozen=True)
class RuleViewer:
    """What one reader, standing at a place, may see of the tenant's agents.

    From inside channel C, read by its own agents only, only C's own agents
    are seen; from anywhere else every agent but such channels' own.
    """

    policy: TenantAccessPolicy
    inside_channel_id: str | None = None
    aliases: Mapping[str, tuple[str | None, ...]] = field(
        default_factory=dict[str, tuple[str | None, ...]]
    )
    """Every name each agent carries (`agent_aliases`), for places that record one."""

    location_channel_id: str | None = None
    location_thread_id: str | None = None
    restrict_to_location: bool = False
    """Member reads also require the agent to run at the verified location.

    No location is outside every explicit agent rule, so omitting a location
    never exposes agents restricted to other channels.
    """

    routed_agent_names: frozenset[str] = frozenset()
    """Effective responders at the location; unrestricted drafts are hidden."""

    @property
    def is_active(self) -> bool:
        return any_own_readers(self.policy) or self.restrict_to_location

    def names_of(self, agent_name: str | None) -> tuple[str | None, ...]:
        """`agent_name` and every other name its agent carries."""
        if agent_name is None:
            return (None,)
        return (agent_name, *self.aliases.get(agent_name, ()))

    def sees_names(self, agent_names: tuple[str | None, ...]) -> bool:
        agent = agent_permissions(self.policy, agent_names)
        if not listed_at(agent, self.inside_channel_id):
            return False
        if not self.restrict_to_location:
            return True
        return runs_at(
            agent,
            channel_permissions(
                self.policy,
                channel_id=self.location_thread_id or self.location_channel_id,
                parent_channel_id=self.location_channel_id if self.location_thread_id else None,
            ),
        ) and (bool(agent.runs_in) or bool(self.routed_agent_names.intersection(agent_names)))

    def sees(self, agent_name: str | None) -> bool:
        """For a place that records one name (a routing row, a binding, a routine)."""
        return self.sees_names(self.names_of(agent_name))

    def sees_agent(self, agent: BetaManagedAgentsAgent) -> bool:
        return self.sees_names(agent_pin_names(agent.name, agent.metadata))

    def sees_place(self, channel_id: str | None) -> bool:
        """A channel (None: a tenant-wide place) on the reader's side of every line."""
        here = channel_permissions(self.policy, channel_id=channel_id)
        return here.home == self.inside_channel_id


async def load_rule_viewer(
    session: AsyncSession,
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    channel_id: str | None,
    is_admin: bool,
    platform: str | None = None,
    thread_id: str | None = None,
    default: DeploymentDefault | None = None,
) -> RuleViewer | None:
    """What a reader at `channel_id` (a thread's parent) sees; None sees everything.

    Admins see everything. Members see agents that may run at this location,
    retaining the visibility boundary of channels kept to their own agents.
    The tenant's agents are listed so a place that records one name counts
    every name its agent carries.
    """
    if is_admin:
        return None
    policy = await load_access_policy(session, tenant_id=tenant_id)
    agents = await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
    return RuleViewer(
        policy,
        home_of(policy, channel_id),
        agent_aliases(agents),
        location_channel_id=channel_id,
        location_thread_id=thread_id,
        restrict_to_location=True,
        routed_agent_names=await load_location_responders(
            session,
            tenant_id=tenant_id,
            channel_id=channel_id,
            platform=platform,
            thread_id=thread_id,
            default=default or DeploymentDefault(),
        ),
    )


async def load_location_responders(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str | None,
    platform: str | None,
    thread_id: str | None,
    default: DeploymentDefault,
) -> frozenset[str]:
    """Effective channel and current thread responders, never the tenant roster.

    Without a verified location there is no proven responder. Resolve the
    real cascade: a channel override hides the workspace/deployment default.
    """
    if channel_id is None:
        return frozenset()
    names: set[str] = set()
    for current_thread in (None, thread_id) if thread_id is not None else (None,):
        try:
            config = await resolve(
                session,
                context=ScopeContext(
                    tenant_id=tenant_id,
                    channel_id=channel_id,
                    platform=platform,
                    thread_id=current_thread,
                ),
                default=default,
            )
        except DaimonError:
            if current_thread is None:
                raise
            # A deleted conversation contributes no active responder. The
            # parent channel's roster remains readable.
            continue
        if config.agent_name is not None:
            names.add(config.agent_name)
    return frozenset(names)


async def is_thread_turn_refused(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str,
    default: DeploymentDefault,
) -> bool:
    """Whether admission would refuse a turn in `thread_id` for a rule.

    Asks `authorize(RUN_AGENT)` of the routed agent before a gate pays for a
    classifier call; admission still decides. The agent is looked up only in
    a channel kept to its own agents, where its other names decide whether it is an own one.
    """
    async with sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
        if not (any_agent_rules(policy) or any_own_readers(policy)):
            return False
        context = ScopeContext(
            tenant_id=tenant_id, channel_id=channel_id, platform=platform, thread_id=thread_id
        )
        try:
            config = await resolve(session, context=context, default=default)
        except DaimonError:  # a deleted binding: admission words that refusal
            return False
    if config.agent_name is None:
        return False
    names: tuple[str | None, ...] = (config.agent_name,)
    if home_of(policy, thread_id, channel_id) is not None:
        agent = await find_agent_by_daimon_tag(
            anthropic, tenant_id=tenant_id, name=config.agent_name
        )
        if agent is not None:
            names = (config.agent_name, *agent_pin_names(agent.name, agent.metadata))
    return not authorize(
        policy,
        subject=Subject(),
        action=Action.RUN_AGENT,
        agent=AgentRef.of(*names),
        place=replace(
            build_turn_place(channel_id=channel_id, thread_id=thread_id),
            setup_thread=config.thread_binding_kind == "setup",
        ),
    )


__all__ = [
    "BindingRefusal",
    "RuleViewer",
    "binding_refusal",
    "render_binding_refusal",
    "clear_refusal",
    "is_memory_hidden",
    "is_thread_turn_refused",
    "is_routine_parent_unknown",
    "keeps_routine_inside",
    "load_rule_viewer",
    "load_location_responders",
    "routine_destination_channel",
    "routine_destination_place",
]
