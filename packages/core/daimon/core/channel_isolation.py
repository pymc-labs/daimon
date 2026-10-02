"""Channel isolation: a sealed channel whose own agents are pinned to it alone.

A server admin isolates channel C (`TenantAccessPolicy.isolated_channel_ids`).
The marker sits on two existing controls: C is sealed, so its content reads
only from inside it, and C's *own agents* are those pinned to C alone
(`daimon.core.access_policy.isolation_owner`), so they answer nowhere else.
The marker adds the rest, decided by `daimon.core.authz.authorize`
(`channel_isolated`): inside C only its own agents run, post, read and get
routines or bindings, and they post nowhere else. This module adds what the
agent and setup surfaces show: outside C its agents are hidden, inside C
only they are seen, and their memory stays writable in C, unlike a plain
seal. A tenant that isolates nothing reads its policy and nothing more.

Everything here is pure but `load_isolation_viewer` and `is_thread_turn_refused`;
`daimon.core.channel_isolation_setup` turns isolation on and off.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import (
    TenantAccessPolicy,
    isolated_channel_of,
    isolation_owner,
    source_seal_ids,
)
from daimon.core.agent_pins import agent_aliases, agent_pin_names
from daimon.core.authz import Action, AgentRef, Place, Subject, authorize, build_turn_place
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from daimon.core.errors import DaimonError
from daimon.core.routine_delivery import delivery_target
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.scoped_config_read import resolve
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BindingRefusal = Literal[
    "agent_confined", "agent_pinned", "channel_needs_own_agent", "channel_isolated"
]
"""Why a routing write would break a pin or isolation.

`agent_confined`: the agent belongs to another isolated channel. `agent_pinned`: the
agent is pinned to other channels, so admission would refuse every turn there.
`channel_needs_own_agent`: the target channel is isolated and the agent is not one of
its own. `channel_isolated`: clearing an isolated channel's own agent would hand it to
a shared one.
"""


def binding_refusal(
    policy: TenantAccessPolicy,
    *,
    agent_names: tuple[str | None, ...],
    channel_id: str | None,
    parent_channel_id: str | None = None,
) -> BindingRefusal | None:
    """Why routing the agent at `channel_id` (None: the tenant default) is refused.

    `authorize(BIND_CHANNEL_DEFAULT)` decides, for everyone: a pinned agent
    routed outside its pin would refuse every turn there. Pass every name the
    agent carries, and a thread binding's parent channel.
    """
    decision = authorize(
        policy,
        subject=Subject(),
        action=Action.BIND_CHANNEL_DEFAULT,
        agent=AgentRef.of(*agent_names),
        place=Place(channel_id=channel_id, parent_channel_id=parent_channel_id),
    )
    if decision.reason == "agent_pinned_elsewhere":
        return "agent_confined" if isolation_owner(policy, agent_names) else "agent_pinned"
    if decision.reason == "channel_isolated":
        return "channel_needs_own_agent"
    return None


def clear_refusal(policy: TenantAccessPolicy, *, channel_id: str) -> BindingRefusal | None:
    return "channel_isolated" if channel_id in policy.isolated_channel_ids else None


def routine_destination_channel(row: RoutineRow) -> str | None:
    """The channel a routine delivers into (a thread's parent), or None without a destination.

    The saved `channel_id` is the destination's parent; a row saved before it
    was recorded falls back to the destination id's channel part.
    """
    if row.destination_kind is None or row.destination_id is None:
        return None
    return row.channel_id or row.destination_id.partition(":")[0]


def is_routine_parent_unknown(row: RoutineRow) -> bool:
    """A thread destination saved before its parent channel was recorded, whose id
    doesn't carry it (a Discord thread; a Slack one is ``channel:ts``)."""
    return (
        row.destination_kind == "thread"
        and row.channel_id is None
        and row.destination_id is not None
        and ":" not in row.destination_id
    )


def routine_destination_place(row: RoutineRow, *, channel_id: str | None) -> Place:
    """Where a routine fires into: `channel_id` under its saved parent channel.

    A thread whose parent is unknown (`is_routine_parent_unknown`) is marked
    `parent_unresolved`, so `authorize` refuses it while anything is isolated.
    """
    return Place(
        channel_id=channel_id,
        parent_channel_id=routine_destination_channel(row),
        parent_unresolved=is_routine_parent_unknown(row),
    )


@dataclass(frozen=True)
class RoutineOrigin:
    """Where a routine's session runs, stamped on it like a turn there."""

    channel_id: str
    thread_id: str | None
    seal_ids: frozenset[str]


def routine_origin(
    policy: TenantAccessPolicy, row: RoutineRow, *, platform: str
) -> RoutineOrigin | None:
    """The channel and thread a routine fires into, with the seal over them now.

    Stamped on the routine's session so its transcript reads only where a
    turn's would: an isolated or sealed channel's routine stays inside it. A
    routine without a destination runs where it was made; one with neither
    is headless (None).
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
        seal_ids=source_seal_ids(policy, channel_id=channel_id, thread_id=thread_id),
    )


def keeps_routine_inside(
    policy: TenantAccessPolicy, row: RoutineRow, *, parent_channel_id: str | None = None
) -> bool:
    """Whether a routine's result must stay in an isolated channel, so never goes by DM.

    The destination decides, so the agent needn't be resolved: an isolated
    channel's own agent fires only into that channel, as its pin refuses every
    other destination, by every name, at save and at each fire. Pass
    `parent_channel_id` once the destination's parent is resolved; until then a
    thread whose parent is unknown stays inside while anything is isolated.
    """
    if isolated_channel_of(policy, parent_channel_id) is not None:
        return True
    if parent_channel_id is None and is_routine_parent_unknown(row):
        return bool(policy.isolated_channel_ids)
    return isolated_channel_of(policy, routine_destination_channel(row)) is not None


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
class ChannelIsolationStatus:
    """The three controls an isolated channel is made of, as the panels show them."""

    is_private: bool
    """Sealed: its messages read only from inside it."""
    dedicated_agent_names: tuple[str, ...]
    """Pinned to this channel alone, so they answer nowhere else."""
    is_hidden: bool
    """Isolated: its own agents are hidden elsewhere, and only they show and answer here."""

    @property
    def is_liftable(self) -> bool:
        """Whether a seal or a dedicated pin is left to lift."""
        return self.is_private or bool(self.dedicated_agent_names)


def channel_isolation_status(policy: TenantAccessPolicy, channel_id: str) -> ChannelIsolationStatus:
    """What of isolation `channel_id` has now. Pure."""
    return ChannelIsolationStatus(
        is_private=channel_id in policy.sealed_channel_ids,
        dedicated_agent_names=tuple(
            sorted(
                name for name, pin in policy.agent_channel_pins.items() if set(pin) == {channel_id}
            )
        ),
        is_hidden=channel_id in policy.isolated_channel_ids,
    )


@dataclass(frozen=True)
class IsolationViewer:
    """What one reader, standing at a place, may see of an isolated tenant.

    From inside isolated channel C only C's own agents are seen; from anywhere
    else every agent but the isolated channels' own.
    """

    policy: TenantAccessPolicy
    inside_channel_id: str | None = None
    aliases: Mapping[str, tuple[str | None, ...]] = field(
        default_factory=dict[str, tuple[str | None, ...]]
    )
    """Every name each agent carries (`agent_aliases`), for places that record one."""

    @property
    def is_active(self) -> bool:
        return bool(self.policy.isolated_channel_ids)

    def names_of(self, agent_name: str | None) -> tuple[str | None, ...]:
        """`agent_name` and every other name its agent carries."""
        if agent_name is None:
            return (None,)
        return (agent_name, *self.aliases.get(agent_name, ()))

    def sees_names(self, agent_names: tuple[str | None, ...]) -> bool:
        return isolation_owner(self.policy, agent_names) == self.inside_channel_id

    def sees(self, agent_name: str | None) -> bool:
        """For a place that records one name (a routing row, a binding, a routine)."""
        return self.sees_names(self.names_of(agent_name))

    def sees_agent(self, agent: BetaManagedAgentsAgent) -> bool:
        return self.sees_names(agent_pin_names(agent.name, agent.metadata))

    def sees_place(self, channel_id: str | None) -> bool:
        """A channel (None: a tenant-wide place) on the reader's side of every line."""
        return isolated_channel_of(self.policy, channel_id) == self.inside_channel_id


async def load_isolation_viewer(
    session: AsyncSession,
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    channel_id: str | None,
    is_admin: bool,
) -> IsolationViewer | None:
    """What a reader at `channel_id` (a thread's parent) sees; None sees everything.

    Admins see everything, and so does everyone while nothing is isolated.
    The tenant's agents are listed so a place that records one name counts
    every name its agent carries.
    """
    if is_admin:
        return None
    policy = await load_access_policy(session, tenant_id=tenant_id)
    if not policy.isolated_channel_ids:
        return None
    agents = await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
    return IsolationViewer(policy, isolated_channel_of(policy, channel_id), agent_aliases(agents))


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
    """Whether admission would refuse a turn in `thread_id` for its pin or isolation.

    Asks `authorize(RUN_AGENT)` of the routed agent before a gate pays for a
    classifier call; admission still decides. The agent is looked up only in
    an isolated channel, where its other names decide whether it is an own one.
    """
    async with sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
        if not (policy.agent_channel_pins or policy.isolated_channel_ids):
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
    if isolated_channel_of(policy, thread_id, channel_id) is not None:
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
    "ChannelIsolationStatus",
    "IsolationViewer",
    "binding_refusal",
    "channel_isolation_status",
    "clear_refusal",
    "is_memory_hidden",
    "is_thread_turn_refused",
    "is_routine_parent_unknown",
    "keeps_routine_inside",
    "load_isolation_viewer",
    "routine_destination_channel",
    "routine_destination_place",
]
