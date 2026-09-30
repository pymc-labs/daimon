"""Agent reach: every place an agent answers, and whether it stays inside some channels.

An agent answers through the config cascade (a channel default, the tenant
default, the deployment fall-through; see `daimon.core.scope.answering_places`)
and through threads bound to it. It is *local to channels S* for a caller when
it is not a tenant-wide default, every channel default and bound thread lies
in S (a thread counts as its parent channel) and no unattended run of it is
owed to someone else with rights beyond the caller's: a server admin, or a
channel admin of a channel outside S. Unattended runs are routines and queued
wakes (timers, handoffs, applied private input); each fires with its
requester's rights, so a stronger requester's would run what the caller writes
with those rights. A plain member's carries only that member's own reach, as
their chat does. Rights are read when the caller edits: a requester promoted
later runs earlier edits with the new rights. An agent that answers nowhere is
local to any S.

A private conversation counts as the channel `/dm` ran in: its DM channel's
row and its `dm:` scope answer only while it is the tenant's live conversation
there, and then as that source channel.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Sequence

from daimon.core.channel_admins import (
    ChannelAdminCaller,
    administered_channel_ids,
    load_administered_channel_ids,
)
from daimon.core.operation_policy import OperationKind, TargetFacts, needs_reachability_read
from daimon.core.scope import (
    AnsweringPlace,
    ChannelConfigRow,
    DeploymentDefault,
    TenantConfigRow,
    answering_places,
)
from daimon.core.stores.channel_admins import list_channel_admins
from daimon.core.stores.direct_messages import DmOrigin, list_dm_origins
from daimon.core.stores.domain import ChannelAdminsRow, UnattendedRequester
from daimon.core.stores.routines import list_routine_creators
from daimon.core.stores.scoped_config_read import (
    is_agent_reachable_in_tenant,
    list_propagations_for_tenant,
)
from daimon.core.stores.task_continuations import list_waiting_requesters
from daimon.core.stores.thread_agent_bindings import (
    list_bound_parent_channel_ids,
    list_dm_bindings,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


class UnattendedRights(BaseModel):
    """The rights an unattended run fires with: its requester's stored role and grants."""

    model_config = ConfigDict(frozen=True)

    platform_user_id: str
    is_server_admin: bool = False
    administered_channel_ids: frozenset[str] = frozenset()

    def exceeds(self, channel_ids: Collection[str]) -> bool:
        """Whether these rights reach beyond a channel admin of `channel_ids`."""
        return self.is_server_admin or not self.administered_channel_ids <= frozenset(channel_ids)


class AgentReach(BaseModel):
    """Where one agent answers (cascade places, bound-thread parents) and who runs it."""

    model_config = ConfigDict(frozen=True)

    agent_name: str
    places: tuple[AnsweringPlace, ...] = ()
    thread_parent_channel_ids: frozenset[str] = frozenset()
    unattended_runs: tuple[UnattendedRights, ...] = ()

    @property
    def is_tenant_wide(self) -> bool:
        """True when the agent is the tenant default or the deployment fall-through."""
        return any(place.tier != "channel" for place in self.places)

    @property
    def channel_ids(self) -> frozenset[str]:
        """Channels the agent answers in: channel defaults plus bound threads' parents."""
        defaults = {place.channel_id for place in self.places if place.channel_id is not None}
        return frozenset(defaults) | self.thread_parent_channel_ids

    def stays_inside(self, channel_ids: Collection[str]) -> bool:
        """Whether every place the agent answers lies in `channel_ids`."""
        return not self.is_tenant_wide and self.channel_ids <= frozenset(channel_ids)

    def runs_unattended_beyond(
        self, channel_ids: Collection[str], *, platform_user_id: str | None
    ) -> bool:
        """Whether someone else with rights beyond `channel_ids` has an unattended run of it."""
        return any(
            run.platform_user_id != platform_user_id and run.exceeds(channel_ids)
            for run in self.unattended_runs
        )

    def is_local_to(self, channel_ids: Collection[str], *, platform_user_id: str | None) -> bool:
        return self.stays_inside(channel_ids) and not self.runs_unattended_beyond(
            channel_ids, platform_user_id=platform_user_id
        )


def build_agent_reach(
    agent_name: str,
    *,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
    thread_parent_channel_ids: Iterable[str] = (),
    unattended_requesters: Iterable[UnattendedRequester] = (),
    grants: Sequence[ChannelAdminsRow] = (),
    dm_origins: Sequence[DmOrigin] = (),
    dm_bindings: Iterable[tuple[str, str, str]] = (),
    unmapped_dms_outside: bool = False,
) -> AgentReach:
    """`grants` are the tenant's channel admin rows, read for requesters' rights.

    `dm_bindings` are the tenant's `(dm_channel_id, scope_id, responder_name)` rows.

    A place in a DM channel, or a `dm:` scope bound to the agent, counts as the
    live conversation's source channel, and not at all without one. With
    `unmapped_dms_outside` (channel isolation) one without a source counts as
    the DM channel itself, a place outside every channel, so an agent still
    bound in an old DM is never taken for one channel's own.
    """
    dm_bindings = tuple(dm_bindings)
    by_channel = {dm.channel_id: dm.origin for dm in dm_origins}
    by_scope = {dm.scope_id: dm.origin for dm in dm_origins}
    dm_channels = by_channel.keys() | {channel for channel, _, _ in dm_bindings}
    places: dict[AnsweringPlace, None] = {}
    for place in answering_places(agent_name, tenant=tenant, channels=channels, default=default):
        if place.channel_id in dm_channels:
            origin = by_channel.get(place.channel_id)
            if origin is None and not unmapped_dms_outside:
                continue
            if origin is not None:
                place = AnsweringPlace(tier="channel", channel_id=origin)
        places[place] = None
    dm_parents = {
        by_scope.get(scope, channel)
        for channel, scope, responder in dm_bindings
        if responder == agent_name and (scope in by_scope or unmapped_dms_outside)
    }
    return AgentReach(
        agent_name=agent_name,
        places=tuple(places),
        thread_parent_channel_ids=frozenset(thread_parent_channel_ids) | dm_parents,
        unattended_runs=tuple(
            UnattendedRights(
                platform_user_id=requester.platform_user_id,
                is_server_admin=requester.is_admin,
                administered_channel_ids=administered_channel_ids(
                    ChannelAdminCaller(
                        platform_user_id=requester.platform_user_id,
                        role_ids=frozenset(requester.role_ids),
                    ),
                    grants,
                ),
            )
            for requester in dict.fromkeys(unattended_requesters)
        ),
    )


async def load_agent_reach(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_name: str,
    default: DeploymentDefault,
) -> AgentReach:
    """Shell half of `build_agent_reach`: read the cascade, bindings, DMs and unattended runs."""
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
        unattended_requesters=[
            *await list_routine_creators(
                session, tenant_id=tenant_id, platform=platform, agent_name=agent_name
            ),
            *await list_waiting_requesters(session, tenant_id=tenant_id, target_name=agent_name),
        ],
        grants=await list_channel_admins(session, tenant_id=tenant_id, platform=platform),
        dm_origins=await list_dm_origins(session, tenant_id=tenant_id),
        dm_bindings=await list_dm_bindings(session, tenant_id=tenant_id),
    )


async def _caller_locality(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_name: str,
    default: DeploymentDefault,
    caller: ChannelAdminCaller,
) -> tuple[bool, bool]:
    """`(local, held_back_by_unattended_run)`; both False without a grant, reading no reach."""
    administered = await load_administered_channel_ids(
        session, tenant_id=tenant_id, platform=platform, caller=caller
    )
    if not administered:
        return False, False
    reach = await load_agent_reach(
        session, tenant_id=tenant_id, platform=platform, agent_name=agent_name, default=default
    )
    inside = reach.stays_inside(administered)
    beyond = reach.runs_unattended_beyond(administered, platform_user_id=caller.platform_user_id)
    return inside and not beyond, inside and beyond


async def is_agent_local_to_caller(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_name: str,
    default: DeploymentDefault,
    caller: ChannelAdminCaller,
) -> bool:
    """True when the caller administers some channels and the agent is local to them.

    False for a caller with no channel admin grant, without reading the reach,
    so a tenant with no channel admins pays one indexed read and nothing else.
    """
    local, _ = await _caller_locality(
        session,
        tenant_id=tenant_id,
        platform=platform,
        agent_name=agent_name,
        default=default,
        caller=caller,
    )
    return local


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
    agents already local to them, one answering nowhere included.
    Never another channel's own agent: that would lend its keys and memory to
    this channel and take its edit rights from that channel's admins.
    """
    if caller.is_server_admin or is_daimon_managed:
        return True
    reach = await load_agent_reach(
        session, tenant_id=tenant_id, platform=platform, agent_name=agent_name, default=default
    )
    if reach.is_tenant_wide:
        return True
    administered = await load_administered_channel_ids(
        session, tenant_id=tenant_id, platform=platform, caller=caller
    )
    return reach.is_local_to(administered, platform_user_id=caller.platform_user_id)


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
    local, held_back = (
        await _caller_locality(
            session,
            tenant_id=tenant_id,
            platform=platform,
            agent_name=agent_name,
            default=default,
            caller=caller,
        )
        if reachable
        else (False, False)
    )
    return TargetFacts(
        is_daimon_managed=is_daimon_managed,
        is_reachable_in_tenant=reachable,
        is_local_to_caller_channels=local,
        runs_unattended_beyond_caller=held_back,
    )


__all__ = [
    "AgentReach",
    "UnattendedRights",
    "build_agent_reach",
    "is_agent_local_to_caller",
    "load_agent_reach",
    "load_target_facts",
    "may_bind_as_channel_default",
]
