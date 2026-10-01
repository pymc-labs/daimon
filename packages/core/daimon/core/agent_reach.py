"""Agent reach: every place an agent answers, and whether it stays inside some channels.

An agent answers through the config cascade (a channel default, the tenant
default, the deployment fall-through; see `daimon.core.scope.answering_places`)
and through threads bound to it. It is *local to channels S* for a caller when
it is not a tenant-wide default, every channel default and bound thread lies
in S (a thread counts as its parent channel) and no routine running it was
made by someone else with rights beyond the caller's: a server admin, or a
channel admin of a channel outside S. A routine fires with its creator's
rights, so such a routine would run what the caller writes with those rights;
a plain member's routine gains nothing. An agent that answers nowhere is local
to any S.

A private conversation counts as the channel `/dm` ran in: its DM channel's
row and its `dm:` scope answer only while it is the tenant's live conversation
there, and then as that source channel.

Skill changes read reach strictly (`operation_policy.needs_strict_reach`): an
agent answers nowhere only with no cascade place, no bound thread, and no
routine or queued continuation of anyone but the caller, whatever their rights.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Sequence

from daimon.core.channel_admins import (
    ChannelAdminCaller,
    administered_channel_ids,
    load_administered_channel_ids,
)
from daimon.core.operation_policy import (
    OperationKind,
    TargetFacts,
    needs_reachability_read,
    needs_strict_reach,
)
from daimon.core.scope import (
    AnsweringPlace,
    ChannelConfigRow,
    DeploymentDefault,
    TenantConfigRow,
    answering_places,
)
from daimon.core.stores.channel_admins import list_channel_admins
from daimon.core.stores.direct_messages import DmOrigin, list_dm_origins
from daimon.core.stores.domain import ChannelAdminsRow
from daimon.core.stores.routines import RoutineCreator, list_routine_creators
from daimon.core.stores.scoped_config_read import (
    is_agent_reachable_in_tenant,
    list_propagations_for_tenant,
)
from daimon.core.stores.task_continuations import list_queued_continuation_requesters
from daimon.core.stores.thread_agent_bindings import (
    list_bound_parent_channel_ids,
    list_dm_bindings,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


class RoutineCreatorRights(BaseModel):
    """The rights a routine's fires run with: its creator's stored role and grants."""

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
    routine_creators: tuple[RoutineCreatorRights, ...] = ()
    continuations: tuple[tuple[str, str], ...] = ()
    """`(requester, parent channel)` of queued continuations that run the agent."""

    @property
    def is_tenant_wide(self) -> bool:
        """True when the agent is the tenant default or the deployment fall-through."""
        return any(place.tier != "channel" for place in self.places)

    @property
    def channel_ids(self) -> frozenset[str]:
        """Channels the agent answers in: channel defaults plus bound threads' parents."""
        defaults = {place.channel_id for place in self.places if place.channel_id is not None}
        return frozenset(defaults) | self.thread_parent_channel_ids

    def is_local_to(self, channel_ids: Collection[str], *, platform_user_id: str | None) -> bool:
        """Whether the agent stays inside `channel_ids`, run by no stronger creator's routine."""
        return (
            not self.is_tenant_wide
            and self.channel_ids <= frozenset(channel_ids)
            and not any(
                creator.platform_user_id != platform_user_id and creator.exceeds(channel_ids)
                for creator in self.routine_creators
            )
        )

    def answers_only_for(self, platform_user_id: str | None) -> bool:
        """No place, no bound thread, and no routine or continuation of anyone else's."""
        return (
            not self.places
            and not self.thread_parent_channel_ids
            and all(c.platform_user_id == platform_user_id for c in self.routine_creators)
            and all(requester == platform_user_id for requester, _ in self.continuations)
        )

    def is_strictly_local_to(
        self, channel_ids: Collection[str], *, platform_user_id: str | None
    ) -> bool:
        """`is_local_to`, and nobody else's continuation posts outside `channel_ids`."""
        return self.is_local_to(channel_ids, platform_user_id=platform_user_id) and all(
            requester == platform_user_id or channel in channel_ids
            for requester, channel in self.continuations
        )


def build_agent_reach(
    agent_name: str,
    *,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
    thread_parent_channel_ids: Iterable[str] = (),
    routine_creators: Iterable[RoutineCreator] = (),
    grants: Sequence[ChannelAdminsRow] = (),
    dm_origins: Sequence[DmOrigin] = (),
    dm_bindings: Iterable[tuple[str, str, str]] = (),
    continuations: Iterable[tuple[str, str]] = (),
) -> AgentReach:
    """`grants` are the tenant's channel admin rows, read for routine creators' rights.

    `dm_bindings` are the tenant's `(dm_channel_id, scope_id, responder_name)` rows.

    A place in a DM channel, or a `dm:` scope bound to the agent, counts as the
    live conversation's source channel, and not at all without one.
    """
    dm_bindings = tuple(dm_bindings)
    by_channel = {dm.channel_id: dm.origin for dm in dm_origins}
    by_scope = {dm.scope_id: dm.origin for dm in dm_origins}
    dm_channels = by_channel.keys() | {channel for channel, _, _ in dm_bindings}
    places: dict[AnsweringPlace, None] = {}
    for place in answering_places(agent_name, tenant=tenant, channels=channels, default=default):
        if place.channel_id in dm_channels:
            origin = by_channel.get(place.channel_id)
            if origin is None:
                continue
            place = AnsweringPlace(tier="channel", channel_id=origin)
        places[place] = None
    dm_parents = {
        by_scope[scope]
        for _, scope, responder in dm_bindings
        if responder == agent_name and scope in by_scope
    }
    return AgentReach(
        agent_name=agent_name,
        places=tuple(places),
        thread_parent_channel_ids=frozenset(thread_parent_channel_ids) | dm_parents,
        routine_creators=tuple(
            RoutineCreatorRights(
                platform_user_id=creator.platform_user_id,
                is_server_admin=creator.is_admin,
                administered_channel_ids=administered_channel_ids(
                    ChannelAdminCaller(
                        platform_user_id=creator.platform_user_id,
                        role_ids=frozenset(creator.role_ids),
                    ),
                    grants,
                ),
            )
            for creator in routine_creators
        ),
        continuations=tuple(continuations),
    )


async def load_agent_reach(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_name: str,
    default: DeploymentDefault,
) -> AgentReach:
    """Shell half of `build_agent_reach`: read the cascade, bindings, DMs and routines."""
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
        routine_creators=await list_routine_creators(
            session, tenant_id=tenant_id, platform=platform, agent_name=agent_name
        ),
        grants=await list_channel_admins(session, tenant_id=tenant_id, platform=platform),
        dm_origins=await list_dm_origins(session, tenant_id=tenant_id),
        dm_bindings=await list_dm_bindings(session, tenant_id=tenant_id),
        continuations=await list_queued_continuation_requesters(
            session, tenant_id=tenant_id, platform=platform, target_name=agent_name
        ),
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
        session, tenant_id=tenant_id, platform=platform, agent_name=agent_name, default=default
    )
    return reach.is_local_to(administered, platform_user_id=caller.platform_user_id)


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
    if needs_strict_reach(operation):
        return await _strict_target_facts(
            session,
            tenant_id=tenant_id,
            platform=platform,
            agent_name=agent_name,
            default=default,
            caller=caller,
            is_daimon_managed=is_daimon_managed,
        )
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


async def _strict_target_facts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_name: str,
    default: DeploymentDefault,
    caller: ChannelAdminCaller,
    is_daimon_managed: bool,
) -> TargetFacts:
    reach = await load_agent_reach(
        session, tenant_id=tenant_id, platform=platform, agent_name=agent_name, default=default
    )
    reachable = not reach.answers_only_for(caller.platform_user_id)
    local = False
    if reachable:
        administered = await load_administered_channel_ids(
            session, tenant_id=tenant_id, platform=platform, caller=caller
        )
        local = bool(administered) and reach.is_strictly_local_to(
            administered, platform_user_id=caller.platform_user_id
        )
    return TargetFacts(
        is_daimon_managed=is_daimon_managed,
        is_reachable_in_tenant=reachable,
        is_local_to_caller_channels=local,
    )


__all__ = [
    "AgentReach",
    "RoutineCreatorRights",
    "build_agent_reach",
    "is_agent_local_to_caller",
    "load_agent_reach",
    "load_target_facts",
    "may_bind_as_channel_default",
]
