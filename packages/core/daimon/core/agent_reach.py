"""Agent reach: every place an agent answers, and whether it stays inside some channels.

An agent answers through the config cascade (a channel default, the tenant
default, the deployment fall-through; see `daimon.core.scope.answering_places`)
and through threads bound to it. It is *local to channels S* when it is not a
tenant-wide default and every channel default and bound thread lies in S (a
thread counts as its parent channel). An agent that answers nowhere is local
to any S.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Sequence

from daimon.core.channel_admins import ChannelAdminCaller, load_administered_channel_ids
from daimon.core.operation_policy import OperationKind, TargetFacts, needs_reachability_read
from daimon.core.scope import (
    AnsweringPlace,
    ChannelConfigRow,
    DeploymentDefault,
    TenantConfigRow,
    answering_places,
)
from daimon.core.stores.scoped_config_read import (
    is_agent_reachable_in_tenant,
    list_propagations_for_tenant,
)
from daimon.core.stores.thread_agent_bindings import list_bound_parent_channel_ids
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


class AgentReach(BaseModel):
    """The cascade places and bound-thread parents through which one agent answers."""

    model_config = ConfigDict(frozen=True)

    agent_name: str
    places: tuple[AnsweringPlace, ...] = ()
    thread_parent_channel_ids: frozenset[str] = frozenset()

    @property
    def is_tenant_wide(self) -> bool:
        """True when the agent is the tenant default or the deployment fall-through."""
        return any(place.tier != "channel" for place in self.places)

    @property
    def channel_ids(self) -> frozenset[str]:
        """Channels the agent answers in: channel defaults plus bound threads' parents."""
        defaults = {place.channel_id for place in self.places if place.channel_id is not None}
        return frozenset(defaults) | self.thread_parent_channel_ids

    def is_local_to(self, channel_ids: Collection[str]) -> bool:
        return not self.is_tenant_wide and self.channel_ids <= frozenset(channel_ids)


def build_agent_reach(
    agent_name: str,
    *,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
    thread_parent_channel_ids: Iterable[str] = (),
) -> AgentReach:
    return AgentReach(
        agent_name=agent_name,
        places=answering_places(agent_name, tenant=tenant, channels=channels, default=default),
        thread_parent_channel_ids=frozenset(thread_parent_channel_ids),
    )


async def load_agent_reach(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_name: str, default: DeploymentDefault
) -> AgentReach:
    """Shell half of `build_agent_reach`: read the tenant's cascade and thread bindings."""
    tenant, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    parents = await list_bound_parent_channel_ids(
        session, tenant_id=tenant_id, responder_name=agent_name
    )
    return build_agent_reach(
        agent_name,
        tenant=tenant,
        channels=channels,
        default=default,
        thread_parent_channel_ids=parents,
    )


async def is_agent_local_to_caller(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_name: str,
    default: DeploymentDefault,
    caller: ChannelAdminCaller,
) -> bool:
    """True when the caller administers some channels and the agent stays inside them.

    False for a caller with no channel admin grant, without reading the reach,
    so a tenant with no channel admins pays one indexed read and nothing else.
    """
    administered = await load_administered_channel_ids(
        session, tenant_id=tenant_id, platform=platform, caller=caller
    )
    if not administered:
        return False
    reach = await load_agent_reach(
        session, tenant_id=tenant_id, agent_name=agent_name, default=default
    )
    return reach.is_local_to(administered)


async def may_bind_as_channel_default(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_name: str,
    default: DeploymentDefault,
    caller: ChannelAdminCaller,
    is_daimon_managed: bool,
) -> bool:
    """Whether `caller` may make `agent_name` the default of a channel they run.

    Server admins bind anything. A channel admin binds only agents shared by
    design (tenant-wide or defaults-managed, which stay read-only to them) or
    agents already local to their channels, one answering nowhere included.
    Never another channel's own agent: that would lend its keys and memory to
    this channel and take its edit rights from that channel's admins.
    """
    if caller.is_server_admin or is_daimon_managed:
        return True
    reach = await load_agent_reach(
        session, tenant_id=tenant_id, agent_name=agent_name, default=default
    )
    if reach.is_tenant_wide:
        return True
    administered = await load_administered_channel_ids(
        session, tenant_id=tenant_id, platform=platform, caller=caller
    )
    return reach.is_local_to(administered)


async def load_target_facts(
    session: AsyncSession,
    operation: OperationKind,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_name: str,
    default: DeploymentDefault,
    caller: ChannelAdminCaller,
    is_daimon_managed: bool,
) -> TargetFacts:
    """The policy facts for one target, reading only what the decision depends on.

    A server admin, a posted-token write or a managed target reads nothing; an
    agent nobody reaches skips the channel admin read.
    """
    if not needs_reachability_read(
        operation, is_admin=caller.is_server_admin, is_daimon_managed=is_daimon_managed
    ):
        return TargetFacts(is_daimon_managed=is_daimon_managed, is_reachable_in_tenant=False)
    reachable = await is_agent_reachable_in_tenant(
        session, tenant_id=tenant_id, agent_name=agent_name, default=default
    )
    local = reachable and await is_agent_local_to_caller(
        session,
        tenant_id=tenant_id,
        platform=platform,
        agent_name=agent_name,
        default=default,
        caller=caller,
    )
    return TargetFacts(
        is_daimon_managed=is_daimon_managed,
        is_reachable_in_tenant=reachable,
        is_local_to_caller_channels=local,
    )


__all__ = [
    "AgentReach",
    "build_agent_reach",
    "is_agent_local_to_caller",
    "load_agent_reach",
    "load_target_facts",
    "may_bind_as_channel_default",
]
