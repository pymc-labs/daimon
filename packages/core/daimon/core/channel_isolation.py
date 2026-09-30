"""Channel isolation: an isolated channel's own agents stay inside it.

A server admin isolates channel C (`TenantAccessPolicy.isolated_channel_ids`).
C's *local agents* are the agents whose every answering place lies in C: the
channel default and handed-over threads under C, never the tenant default
(see `daimon.core.agent_reach`). While C is isolated they may not be bound
anywhere else, they are invisible from outside C, and from inside C only
they are visible. A call is *inside C* when it runs in C or its threads, or
when the agent executing it is C-local. Everything else is outside every
isolated channel. A tenant that isolates nothing loads `NO_ISOLATION` and
nothing changes.

`build_channel_isolation` and the refusal rules are pure; `load_channel_isolation`
is their shell.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping
from typing import Literal

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_reach import build_agent_reach
from daimon.core.scope import ChannelConfigRow, DeploymentDefault, TenantConfigRow
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.core.stores.thread_agent_bindings import list_handoff_parent_channel_ids
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


def build_channel_isolation(
    isolated_channel_ids: Collection[str],
    *,
    tenant: TenantConfigRow | None,
    channels: Collection[ChannelConfigRow],
    default: DeploymentDefault,
    thread_parent_channel_ids: Mapping[str, Collection[str]],
) -> ChannelIsolation:
    isolated = frozenset(isolated_channel_ids)
    if not isolated:
        return NO_ISOLATION
    names = {row.agent_name for row in channels if row.agent_name} | set(thread_parent_channel_ids)
    names |= {name for name in (tenant.agent_name if tenant else None, default.agent_name) if name}
    confined: dict[str, str] = {}
    answering: set[str] = set()
    for name in sorted(names):
        reach = build_agent_reach(
            name,
            tenant=tenant,
            channels=list(channels),
            default=default,
            thread_parent_channel_ids=thread_parent_channel_ids.get(name, ()),
        )
        if reach.places or reach.thread_parent_channel_ids:
            answering.add(name)
        if not reach.is_tenant_wide and len(reach.channel_ids) == 1:
            (only,) = reach.channel_ids
            if only in isolated:
                confined[name] = only
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
) -> ChannelIsolation:
    """Shell half: one policy read when nothing is isolated, the cascade when something is.

    Pass `policy` when the caller already holds it. An unreadable policy
    raises `AccessPolicyUnreadable`, so callers refuse rather than fall open.
    """
    if policy is None:
        policy = await load_access_policy(session, tenant_id=tenant_id)
    if not policy.isolated_channel_ids:
        return NO_ISOLATION
    tenant, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    parents = await list_handoff_parent_channel_ids(session, tenant_id=tenant_id)
    return build_channel_isolation(
        policy.isolated_channel_ids,
        tenant=tenant,
        channels=channels,
        default=default,
        thread_parent_channel_ids=parents,
    )


__all__ = [
    "NO_ISOLATION",
    "BindingRefusal",
    "ChannelIsolation",
    "build_channel_isolation",
    "load_channel_isolation",
]
