"""Channel isolation: an isolated channel's own agents stay inside it.

A server admin isolates channel C (`TenantAccessPolicy.isolated_channel_ids`).
C's *own agents* are those whose reach stays inside C
(`daimon.core.agent_reach.AgentReach.stays_inside`): the channel default and
handed-over threads under C, never the tenant default, and a DM with no
recorded source counts as outside C. While C is isolated they may not be bound
anywhere else, they are invisible from outside C, and from inside C only
they are visible. A call is *inside C* when it runs in C or its threads, or
when the agent executing it is C-local. Everything else is outside every
isolated channel. A tenant that isolates nothing loads `NO_ISOLATION` and
nothing changes.

`build_channel_isolation` and the refusal rules are pure; `load_channel_isolation`
is their shell. Admission, the scheduler and the routine posters enforce it at
run time; the routing writes and the tools keep it from being set up wrong.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_reach import build_agent_reach
from daimon.core.scope import ChannelConfigRow, DeploymentDefault, TenantConfigRow
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.direct_messages import DmOrigin, list_dm_origins
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.core.stores.thread_agent_bindings import (
    list_dm_bindings,
    list_handoff_parent_channel_ids,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

BindingRefusal = Literal["agent_confined", "channel_needs_own_agent", "channel_isolated"]
"""Why a routing write would break isolation.

`agent_confined`: the agent belongs to another isolated channel. `channel_needs_own_agent`:
the target channel is isolated and the agent already answers elsewhere (or is built in).
`channel_isolated`: clearing an isolated channel's own agent would hand it to a shared one.
"""


class ChannelIsolation(BaseModel):
    """The tenant's isolated channels and the agents confined to each."""

    model_config = ConfigDict(frozen=True)

    isolated_channel_ids: frozenset[str] = frozenset()
    agent_channel_ids: Mapping[str, str] = {}
    """Agent name -> the one isolated channel it is local to."""
    answering_agent_names: frozenset[str] = frozenset()
    """Every agent that answers somewhere; the rest answer nowhere."""

    @property
    def is_active(self) -> bool:
        return bool(self.isolated_channel_ids)

    def channel_of(self, agent_name: str | None) -> str | None:
        """The isolated channel `agent_name` is confined to, or None for a shared agent."""
        return None if agent_name is None else self.agent_channel_ids.get(agent_name)

    def isolated_channel(
        self, channel_id: str | None, parent_channel_id: str | None = None
    ) -> str | None:
        """The isolated channel a location lies in (a thread counts as its parent)."""
        for candidate in (parent_channel_id, channel_id):
            if candidate is not None and candidate in self.isolated_channel_ids:
                return candidate
        return None

    def is_visible(self, agent_name: str, *, inside_channel_id: str | None) -> bool:
        """Visible from inside C only when C-local; from outside only when shared."""
        return self.channel_of(agent_name) == inside_channel_id

    def crosses(
        self, agent_name: str, channel_id: str | None, parent_channel_id: str | None = None
    ) -> bool:
        """Whether `agent_name` acting at a place (None: a DM or no channel) crosses a line.

        C's own agents act only inside C, and inside C only they act.
        """
        return self.channel_of(agent_name) != self.isolated_channel(channel_id, parent_channel_id)

    def routine_crosses(self, row: RoutineRow) -> bool:
        """Whether a routine delivers across a line.

        One without a destination reports by DM, which is outside every channel.
        """
        return self.crosses(row.agent_name, routine_destination_channel(row))

    def keeps_routine_inside(self, row: RoutineRow) -> bool:
        """Whether a routine's result must stay in an isolated channel, so never goes by DM."""
        return (
            self.channel_of(row.agent_name) is not None
            or self.isolated_channel(routine_destination_channel(row)) is not None
        )

    def binding_refusal(
        self, agent_name: str, *, channel_id: str | None, is_daimon_managed: bool = False
    ) -> BindingRefusal | None:
        """Why routing `agent_name` at `channel_id` (None: the tenant default) is refused.

        Pass a thread's parent channel for a thread binding. An isolated channel
        takes only its own agents, or one that answers nowhere yet and so
        becomes its own; a built-in agent never does.
        """
        target = self.isolated_channel(channel_id)
        owner = self.channel_of(agent_name)
        if owner is not None and owner != target:
            return "agent_confined"
        taken = is_daimon_managed or agent_name in self.answering_agent_names
        if target is not None and owner != target and taken:
            return "channel_needs_own_agent"
        return None

    def clear_refusal(self, *, channel_id: str) -> BindingRefusal | None:
        return "channel_isolated" if channel_id in self.isolated_channel_ids else None


NO_ISOLATION = ChannelIsolation()


def routine_destination_channel(row: RoutineRow) -> str | None:
    """The channel a routine delivers into (a thread's parent), or None without a destination.

    The saved `channel_id` is the destination's parent; a row saved before it
    was recorded falls back to the destination id's channel part.
    """
    if row.destination_kind is None or row.destination_id is None:
        return None
    return row.channel_id or row.destination_id.partition(":")[0]


@dataclass(frozen=True)
class IsolationViewer:
    """What one reader, standing at a place, may see of an isolated tenant."""

    isolation: ChannelIsolation
    inside_channel_id: str | None = None

    def sees(self, agent_name: str) -> bool:
        return self.isolation.is_visible(agent_name, inside_channel_id=self.inside_channel_id)

    def sees_place(self, channel_id: str | None) -> bool:
        """A channel (None: a tenant-wide place) on the reader's side of every line."""
        return self.isolation.isolated_channel(channel_id) == self.inside_channel_id


def build_channel_isolation(
    isolated_channel_ids: Collection[str],
    *,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
    thread_parent_channel_ids: Mapping[str, Collection[str]],
    dm_origins: Sequence[DmOrigin] = (),
    dm_bindings: Iterable[tuple[str, str, str]] = (),
) -> ChannelIsolation:
    isolated = frozenset(isolated_channel_ids)
    if not isolated:
        return NO_ISOLATION
    dm_bindings = tuple(dm_bindings)
    names = {row.agent_name for row in channels if row.agent_name} | set(thread_parent_channel_ids)
    names |= {responder for _, _, responder in dm_bindings}
    names |= {name for name in (tenant.agent_name if tenant else None, default.agent_name) if name}
    confined: dict[str, str] = {}
    answering: set[str] = set()
    for name in sorted(names):
        reach = build_agent_reach(
            name,
            tenant=tenant,
            channels=channels,
            default=default,
            thread_parent_channel_ids=thread_parent_channel_ids.get(name, ()),
            dm_origins=dm_origins,
            dm_bindings=dm_bindings,
            unmapped_dms_outside=True,
        )
        if reach.places or reach.thread_parent_channel_ids:
            answering.add(name)
        owner = next((c for c in reach.channel_ids if c in isolated), None)
        if owner is not None and reach.stays_inside({owner}):
            confined[name] = owner
    return ChannelIsolation(
        isolated_channel_ids=isolated,
        agent_channel_ids=confined,
        answering_agent_names=frozenset(answering),
    )


async def load_channel_isolation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    default: DeploymentDefault,
    policy: TenantAccessPolicy | None = None,
    isolated_channel_ids: Collection[str] | None = None,
) -> ChannelIsolation:
    """Shell half: one policy read when nothing is isolated, the cascade when something is.

    Pass `policy` when the caller already holds it, or `isolated_channel_ids`
    to ask what isolating those would mean. An unreadable policy raises
    `AccessPolicyUnreadable`, so callers refuse rather than fall open.
    """
    if isolated_channel_ids is None:
        if policy is None:
            policy = await load_access_policy(session, tenant_id=tenant_id)
        isolated_channel_ids = policy.isolated_channel_ids
    if not isolated_channel_ids:
        return NO_ISOLATION
    tenant, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    return build_channel_isolation(
        isolated_channel_ids,
        tenant=tenant,
        channels=channels,
        default=default,
        thread_parent_channel_ids=await list_handoff_parent_channel_ids(
            session, tenant_id=tenant_id
        ),
        dm_origins=await list_dm_origins(session, tenant_id=tenant_id),
        dm_bindings=await list_dm_bindings(session, tenant_id=tenant_id),
    )


async def load_isolation_viewer(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    default: DeploymentDefault,
    channel_id: str | None,
    is_admin: bool,
) -> IsolationViewer | None:
    """What a reader at `channel_id` (a thread's parent) sees; None sees everything.

    Admins see everything, and so does everyone while nothing is isolated.
    """
    if is_admin:
        return None
    isolation = await load_channel_isolation(session, tenant_id=tenant_id, default=default)
    if not isolation.is_active:
        return None
    return IsolationViewer(isolation, isolation.isolated_channel(channel_id))


__all__ = [
    "NO_ISOLATION",
    "BindingRefusal",
    "ChannelIsolation",
    "IsolationViewer",
    "build_channel_isolation",
    "load_channel_isolation",
    "load_isolation_viewer",
    "routine_destination_channel",
]
