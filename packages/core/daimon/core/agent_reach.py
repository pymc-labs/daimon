"""Agent reach: every place an agent answers, and whether it stays inside some channels.

An agent answers through the config cascade (a channel default, the tenant
default, the deployment fall-through; see `daimon.core.scope.answering_places`)
and through threads bound to it, under every name it carries (its own name
and its routing name) and by its stable id. It is *local to channels S* for a
caller when it is not a tenant-wide or anyone's personal default, every channel
default and bound thread lies in S (a thread counts as its parent channel) and
no unattended run of it is owed to someone else with rights beyond the caller's:
a server admin, or a channel admin of a channel outside S. Unattended runs are
routines and queued wakes (timers, handoffs, applied private input); each fires
with its requester's rights, so a stronger requester's would run what the caller
writes with those rights. A plain member's carries only that member's own reach,
as their chat does. Rights are those stored at the requester's last chat turn,
read when the caller edits: a requester promoted later runs earlier edits with
the new rights. An agent that answers nowhere is local to any S; one with no
name at all is local to none.

A private conversation counts as the channel `/dm` ran in: its DM channel's
row and its `dm:` scope answer only while it is the tenant's live conversation
there, and then as that source channel.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Sequence
from typing import Final

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
    is_agent_reachable,
)
from daimon.core.stores.channel_admins import list_channel_admins
from daimon.core.stores.direct_messages import DmOrigin, list_dm_origins
from daimon.core.stores.domain import ChannelAdminsRow, UnattendedRequester
from daimon.core.stores.routines import list_routine_creators
from daimon.core.stores.scoped_config_read import (
    has_personal_default,
    is_agent_shared_for_key_changes,
    list_propagations_for_tenant,
)
from daimon.core.stores.task_continuations import list_waiting_requesters
from daimon.core.stores.thread_agent_bindings import (
    list_bound_parent_channel_ids,
    list_dm_bindings,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

KEY_CHANGE_OPERATIONS: Final[frozenset[OperationKind]] = frozenset(
    {"key_replace", "key_remove", "mcp_replace", "mcp_remove"}
)
"""Read as shared by `is_agent_shared_for_key_changes`: the keys and servers also
reach routines and live sessions. Spec edits and repo binds read the cascade only."""


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

    agent_names: tuple[str, ...]
    places: tuple[AnsweringPlace, ...] = ()
    thread_parent_channel_ids: frozenset[str] = frozenset()
    unattended_runs: tuple[UnattendedRights, ...] = ()
    is_personal_default: bool = False

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
        """Whether every place the agent answers lies in `channel_ids`.

        A personal default answers its member everywhere, so it never does.
        """
        return (
            bool(self.agent_names)
            and not self.is_tenant_wide
            and not self.is_personal_default
            and self.channel_ids <= frozenset(channel_ids)
        )

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
    agent_names: tuple[str, ...],
    *,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
    ma_agent_id: str | None = None,
    thread_parent_channel_ids: Iterable[str] = (),
    unattended_requesters: Iterable[UnattendedRequester] = (),
    grants: Sequence[ChannelAdminsRow] = (),
    dm_origins: Sequence[DmOrigin] = (),
    dm_bindings: Iterable[tuple[str, str, str, str]] = (),
    is_personal_default: bool = False,
) -> AgentReach:
    """`grants` are the tenant's channel admin rows, read for requesters' rights.

    `dm_bindings` are the tenant's `(dm_channel_id, scope_id, responder_name,
    responder_ma_agent_id)` rows; one counts under any of `agent_names` or
    `ma_agent_id`.

    A place in a DM channel, or a `dm:` scope bound to the agent, counts as the
    live conversation's source channel, and not at all without one.
    """
    names = tuple(dict.fromkeys(name for name in agent_names if name))
    dm_bindings = tuple(dm_bindings)
    by_channel = {dm.channel_id: dm.origin for dm in dm_origins}
    by_scope = {dm.scope_id: dm.origin for dm in dm_origins}
    dm_channels = by_channel.keys() | {binding[0] for binding in dm_bindings}
    places: dict[AnsweringPlace, None] = {}
    for name in names:
        for place in answering_places(name, tenant=tenant, channels=channels, default=default):
            if place.channel_id in dm_channels:
                origin = by_channel.get(place.channel_id)
                if origin is None:
                    continue
                place = AnsweringPlace(tier="channel", channel_id=origin)
            places[place] = None
    dm_parents = {
        by_scope[scope]
        for _, scope, responder, responder_id in dm_bindings
        if (responder in names or (ma_agent_id is not None and responder_id == ma_agent_id))
        and scope in by_scope
    }
    return AgentReach(
        agent_names=names,
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
        is_personal_default=is_personal_default,
    )


async def load_agent_reach(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_names: tuple[str, ...],
    ma_agent_id: str | None,
    default: DeploymentDefault,
) -> AgentReach:
    """Shell half of `build_agent_reach`: read the cascade, bindings, DMs and unattended runs."""
    names = tuple(dict.fromkeys(name for name in agent_names if name))
    tenant, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    parents = await list_bound_parent_channel_ids(
        session, tenant_id=tenant_id, responder_names=names, responder_ma_agent_id=ma_agent_id
    )
    return build_agent_reach(
        names,
        tenant=tenant,
        channels=channels,
        default=default,
        ma_agent_id=ma_agent_id,
        thread_parent_channel_ids=parents,
        unattended_requesters=[
            *await list_routine_creators(
                session,
                tenant_id=tenant_id,
                platform=platform,
                agent_names=names,
                agent_id=ma_agent_id,
            ),
            *await list_waiting_requesters(
                session, tenant_id=tenant_id, target_names=names, target_ma_agent_id=ma_agent_id
            ),
        ],
        grants=await list_channel_admins(session, tenant_id=tenant_id, platform=platform),
        dm_origins=await list_dm_origins(session, tenant_id=tenant_id),
        dm_bindings=await list_dm_bindings(session, tenant_id=tenant_id),
        is_personal_default=await has_personal_default(
            session, tenant_id=tenant_id, agent_names=names
        ),
    )


async def _caller_locality(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_names: tuple[str, ...],
    ma_agent_id: str | None,
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
        session,
        tenant_id=tenant_id,
        platform=platform,
        agent_names=agent_names,
        ma_agent_id=ma_agent_id,
        default=default,
    )
    inside = reach.stays_inside(administered)
    beyond = reach.runs_unattended_beyond(administered, platform_user_id=caller.platform_user_id)
    return inside and not beyond, inside and beyond


async def is_agent_local_to_caller(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_names: tuple[str, ...],
    ma_agent_id: str | None,
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
        agent_names=agent_names,
        ma_agent_id=ma_agent_id,
        default=default,
        caller=caller,
    )
    return local


async def may_bind_as_channel_default(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_names: tuple[str, ...],
    ma_agent_id: str | None,
    default: DeploymentDefault,
    caller: ChannelAdminCaller,
    is_daimon_managed: bool,
) -> bool:
    """Whether `caller` may make the agent the default of a channel they run.

    Server admins bind anything. A channel admin binds only
    agents shared by design (tenant-wide or defaults-managed, which stay
    read-only to them) or agents already local to them, one answering nowhere
    included. Never another channel's own agent: that would lend its keys and
    memory to this channel and take its edit rights from that channel's admins.
    """
    if caller.is_server_admin or is_daimon_managed:
        return True
    reach = await load_agent_reach(
        session,
        tenant_id=tenant_id,
        platform=platform,
        agent_names=agent_names,
        ma_agent_id=ma_agent_id,
        default=default,
    )
    if reach.is_tenant_wide:
        return True
    administered = await load_administered_channel_ids(
        session, tenant_id=tenant_id, platform=platform, caller=caller
    )
    return reach.is_local_to(administered, platform_user_id=caller.platform_user_id)


async def _is_shared(
    session: AsyncSession,
    operation: OperationKind,
    *,
    tenant_id: uuid.UUID,
    agent_names: tuple[str, ...],
    ma_agent_id: str | None,
    default: DeploymentDefault,
    caller_account_id: uuid.UUID | None,
    caller_platform_user_id: str | None,
) -> bool:
    """The reachability fact, read as wide as the operation needs. No name fails closed."""
    if not agent_names:
        return True
    if operation in KEY_CHANGE_OPERATIONS:
        # No stable id to look routines, sessions and bindings up by: fail closed.
        return ma_agent_id is None or await is_agent_shared_for_key_changes(
            session,
            tenant_id=tenant_id,
            agent_names=agent_names,
            ma_agent_id=ma_agent_id,
            default=default,
            caller_account_id=caller_account_id,
            caller_platform_user_id=caller_platform_user_id,
        )
    tenant, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    return any(
        is_agent_reachable(name, tenant=tenant, channels=channels, default=default)
        for name in agent_names
    )


async def load_target_facts(
    session: AsyncSession,
    operation: OperationKind,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_names: tuple[str | None, ...],
    ma_agent_id: str | None,
    default: DeploymentDefault,
    caller: ChannelAdminCaller,
    is_daimon_managed: bool,
    caller_account_id: uuid.UUID | None = None,
    caller_platform_user_id: str | None = None,
) -> TargetFacts:
    """The policy facts for one target, reading only what the decision depends on.

    A server admin, a posted-token write or a managed target reads nothing; an
    agent nobody reaches skips the channel admin read. `agent_names` is every
    name the agent carries (`daimon.core.agent_pins.agent_pin_names`). The
    caller ids leave the caller's own routines and sessions out of a key
    change's sharing; None counts them all.
    """
    if not needs_reachability_read(
        operation, is_admin=caller.is_server_admin, is_daimon_managed=is_daimon_managed
    ):
        return TargetFacts(is_daimon_managed=is_daimon_managed, is_reachable_in_tenant=False)
    names = tuple(dict.fromkeys(name for name in agent_names if name))
    reachable = await _is_shared(
        session,
        operation,
        tenant_id=tenant_id,
        agent_names=names,
        ma_agent_id=ma_agent_id,
        default=default,
        caller_account_id=caller_account_id,
        caller_platform_user_id=caller_platform_user_id,
    )
    local, held_back = (
        await _caller_locality(
            session,
            tenant_id=tenant_id,
            platform=platform,
            agent_names=names,
            ma_agent_id=ma_agent_id,
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
    "KEY_CHANGE_OPERATIONS",
    "AgentReach",
    "UnattendedRights",
    "build_agent_reach",
    "is_agent_local_to_caller",
    "load_agent_reach",
    "load_target_facts",
    "may_bind_as_channel_default",
]
