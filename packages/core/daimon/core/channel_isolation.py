"""Channel isolation: a sealed channel whose own agents are pinned to it alone.

A server admin isolates channel C (`TenantAccessPolicy.isolated_channel_ids`).
The marker sits on two existing controls: C is sealed, so its content reads
only from inside it, and C's *own agents* are those pinned to C alone
(`daimon.core.access_policy.isolation_owner`), so they answer nowhere else.
The marker adds the rest: inside C only its own agents answer and are seen,
outside C they are hidden, and their memory stays writable in C, unlike a
plain seal. A tenant that isolates nothing reads its policy and nothing more.

Everything here is pure but `load_isolation_viewer` and `is_thread_turn_refused`;
`daimon.core.channel_isolation_setup` turns isolation on and off.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import (
    TenantAccessPolicy,
    is_outside_agent_pin,
    isolated_channel_of,
    isolation_owner,
)
from daimon.core.agent_pins import agent_pin_names
from daimon.core.errors import DaimonError
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.scoped_config_read import resolve
from sqlalchemy.ext.asyncio import AsyncSession

BindingRefusal = Literal["agent_confined", "channel_needs_own_agent", "channel_isolated"]
"""Why a routing write would break isolation.

`agent_confined`: the agent belongs to another isolated channel. `channel_needs_own_agent`:
the target channel is isolated and the agent is not one of its own.
`channel_isolated`: clearing an isolated channel's own agent would hand it to a shared one.
"""


def is_refused_by_isolation(
    policy: TenantAccessPolicy,
    *,
    agent_names: tuple[str | None, ...],
    channel_id: str | None,
    parent_channel_id: str | None = None,
    is_setup_thread: bool = False,
) -> bool:
    """Whether a turn in an isolated channel is answered by an agent not its own.

    The reverse, an own agent answering elsewhere, is the pin's to refuse. A
    setup thread answers as the built-in agent and is let through.
    """
    inside = isolated_channel_of(policy, channel_id, parent_channel_id)
    return (
        inside is not None
        and not is_setup_thread
        and isolation_owner(policy, agent_names) != inside
    )


def binding_refusal(
    policy: TenantAccessPolicy, *, agent_names: tuple[str | None, ...], channel_id: str | None
) -> BindingRefusal | None:
    """Why routing the agent at `channel_id` (None: the tenant default) is refused.

    Pass a thread's parent channel for a thread binding.
    """
    target = isolated_channel_of(policy, channel_id)
    owner = isolation_owner(policy, agent_names)
    if owner is not None and owner != target:
        return "agent_confined"
    if target is not None and owner != target:
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


def keeps_routine_inside(policy: TenantAccessPolicy, row: RoutineRow) -> bool:
    """Whether a routine's result must stay in an isolated channel, so never goes by DM."""
    return (
        isolation_owner(policy, (row.agent_name,)) is not None
        or isolated_channel_of(policy, routine_destination_channel(row)) is not None
    )


def is_memory_hidden(
    policy: TenantAccessPolicy,
    *,
    agent_names: tuple[str | None, ...],
    channel_id: str,
    parent_channel_id: str | None = None,
) -> bool:
    """Whether an agent's memory stays hidden at a place: outside its pin, or across a line."""
    inside = isolated_channel_of(policy, channel_id, parent_channel_id)
    return isolation_owner(policy, agent_names) != inside or is_outside_agent_pin(
        policy, agent_names=agent_names, channel_id=channel_id, parent_channel_id=parent_channel_id
    )


@dataclass(frozen=True)
class IsolationViewer:
    """What one reader, standing at a place, may see of an isolated tenant.

    From inside isolated channel C only C's own agents are seen; from anywhere
    else every agent but the isolated channels' own.
    """

    policy: TenantAccessPolicy
    inside_channel_id: str | None = None

    @property
    def is_active(self) -> bool:
        return bool(self.policy.isolated_channel_ids)

    def sees_names(self, agent_names: tuple[str | None, ...]) -> bool:
        return isolation_owner(self.policy, agent_names) == self.inside_channel_id

    def sees(self, agent_name: str | None) -> bool:
        """For a place that records only a routing name; prefer `sees_agent`."""
        return self.sees_names((agent_name,))

    def sees_agent(self, agent: BetaManagedAgentsAgent) -> bool:
        return self.sees_names(agent_pin_names(agent.name, agent.metadata))

    def sees_place(self, channel_id: str | None) -> bool:
        """A channel (None: a tenant-wide place) on the reader's side of every line."""
        return isolated_channel_of(self.policy, channel_id) == self.inside_channel_id


async def load_isolation_viewer(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel_id: str | None, is_admin: bool
) -> IsolationViewer | None:
    """What a reader at `channel_id` (a thread's parent) sees; None sees everything.

    Admins see everything, and so does everyone while nothing is isolated.
    """
    if is_admin:
        return None
    policy = await load_access_policy(session, tenant_id=tenant_id)
    if not policy.isolated_channel_ids:
        return None
    return IsolationViewer(policy, isolated_channel_of(policy, channel_id))


async def is_thread_turn_refused(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str,
    default: DeploymentDefault,
) -> bool:
    """Whether admission would refuse a turn in `thread_id` for its pin or isolation.

    Read from routing and the policy alone, with no agent lookup, so a gate
    can skip paid work first; admission still decides.
    """
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
    names = (config.agent_name,)
    is_setup_thread = config.thread_binding_kind == "setup"
    return (
        not is_setup_thread
        and is_outside_agent_pin(
            policy, agent_names=names, channel_id=thread_id, parent_channel_id=channel_id
        )
    ) or is_refused_by_isolation(
        policy,
        agent_names=names,
        channel_id=thread_id,
        parent_channel_id=channel_id,
        is_setup_thread=is_setup_thread,
    )


__all__ = [
    "BindingRefusal",
    "IsolationViewer",
    "binding_refusal",
    "clear_refusal",
    "is_memory_hidden",
    "is_refused_by_isolation",
    "is_thread_turn_refused",
    "keeps_routine_inside",
    "load_isolation_viewer",
    "routine_destination_channel",
]
