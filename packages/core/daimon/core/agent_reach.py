"""Where an agent answers and whether its runs stay inside a set of channels.

Channel rules use this reach to keep an agent scoped to its own channel. DM
bindings count at their recorded source channel; unplaced runs fail closed.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Sequence

from daimon.core.admin_actions import DelegatedAction
from daimon.core.agent_ownership import owns_agent
from daimon.core.native_admins import AdminCaller, load_live_subject
from daimon.core.operation_policy import OperationKind, TargetFacts
from daimon.core.scope import (
    AnsweringPlace,
    ChannelConfigRow,
    DeploymentDefault,
    TenantConfigRow,
    answering_places,
)
from daimon.core.stores.direct_messages import DmOrigin, list_dm_origins
from daimon.core.stores.routines import list_routine_channel_ids
from daimon.core.stores.scoped_config_read import has_personal_default, list_propagations_for_tenant
from daimon.core.stores.thread_agent_bindings import (
    list_bound_parent_channel_ids,
    list_dm_bindings,
)
from daimon.core.stores.thread_sessions import list_live_session_channel_ids
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


class AgentReach(BaseModel):
    """An agent's answering places and other people's recorded runs."""

    model_config = ConfigDict(frozen=True)

    agent_names: tuple[str, ...]
    places: tuple[AnsweringPlace, ...] = ()
    thread_parent_channel_ids: frozenset[str] = frozenset()
    run_channel_ids: frozenset[str] = frozenset()
    has_unplaced_run: bool = False
    is_personal_default: bool = False

    @property
    def is_tenant_wide(self) -> bool:
        return any(place.tier != "channel" for place in self.places)

    @property
    def channel_ids(self) -> frozenset[str]:
        defaults = {place.channel_id for place in self.places if place.channel_id is not None}
        return frozenset(defaults) | self.thread_parent_channel_ids | self.run_channel_ids

    def stays_inside(self, channel_ids: Collection[str]) -> bool:
        """True when every known answer and run stays inside these channels."""
        return (
            bool(self.agent_names)
            and not self.is_tenant_wide
            and not self.is_personal_default
            and not self.has_unplaced_run
            and self.channel_ids <= frozenset(channel_ids)
        )


def build_agent_reach(
    agent_names: tuple[str, ...],
    *,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
    ma_agent_id: str | None = None,
    thread_parent_channel_ids: Iterable[str] = (),
    run_channel_ids: Iterable[str | None] = (),
    dm_origins: Sequence[DmOrigin] = (),
    dm_bindings: Iterable[tuple[str, str, str, str]] = (),
    is_personal_default: bool = False,
) -> AgentReach:
    """Build reach from the config cascade, bindings, DMs and recorded runs."""
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
    runs = set(run_channel_ids)
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
        run_channel_ids=frozenset(channel for channel in runs if channel is not None),
        has_unplaced_run=None in runs,
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
    caller_account_id: uuid.UUID | None = None,
    caller_platform_user_id: str | None = None,
) -> AgentReach:
    """Read an agent's answering places and recorded runs."""
    names = tuple(dict.fromkeys(name for name in agent_names if name))
    tenant, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    parents = await list_bound_parent_channel_ids(
        session, tenant_id=tenant_id, responder_names=names, responder_ma_agent_id=ma_agent_id
    )
    sessions = (
        await list_live_session_channel_ids(
            session,
            tenant_id=tenant_id,
            ma_agent_id=ma_agent_id,
            caller_account_id=caller_account_id,
        )
        if ma_agent_id is not None
        else []
    )
    routines = await list_routine_channel_ids(
        session,
        tenant_id=tenant_id,
        agent_names=names,
        agent_id=ma_agent_id,
        caller_platform_user_id=caller_platform_user_id,
    )
    return build_agent_reach(
        names,
        tenant=tenant,
        channels=channels,
        default=default,
        ma_agent_id=ma_agent_id,
        thread_parent_channel_ids=parents,
        run_channel_ids=[*sessions, *routines],
        dm_origins=await list_dm_origins(session, tenant_id=tenant_id),
        dm_bindings=await list_dm_bindings(session, tenant_id=tenant_id),
        is_personal_default=await has_personal_default(
            session, tenant_id=tenant_id, agent_names=names
        ),
    )


async def may_bind_as_channel_default(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    agent_names: tuple[str, ...],
    ma_agent_id: str | None,
    default: DeploymentDefault,
    caller: AdminCaller,
    is_daimon_managed: bool,
    caller_account_id: uuid.UUID | None = None,
) -> bool:
    """A Daimon admin may bind an agent once its channel rule passes."""
    return (
        await load_live_subject(
            session,
            tenant_id=tenant_id,
            platform=platform,
            caller=caller,
            action=DelegatedAction.ROUTING,
        )
    ).is_admin


async def load_target_facts(
    session: AsyncSession,
    operation: OperationKind,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_names: tuple[str | None, ...],
    ma_agent_id: str | None,
    default: DeploymentDefault,
    caller: AdminCaller,
    is_daimon_managed: bool,
    caller_account_id: uuid.UUID | None = None,
    caller_platform_user_id: str | None = None,
) -> TargetFacts:
    """Whether the target is managed, and whether the caller owns it.

    The caller is the person acting: `caller_platform_user_id` (or the
    `caller`'s) on `platform`, the one who actually clicked or submitted.
    `caller_account_id` counts only when no platform user is known, since some
    submit paths pass the requester's account while someone else submits.
    Callers pass only a person, never an agent key.
    """
    platform_user_id = caller_platform_user_id or caller.platform_user_id
    return TargetFacts(
        is_daimon_managed=is_daimon_managed,
        is_reachable_in_tenant=False,
        owned_by_caller=not is_daimon_managed
        and await owns_agent(
            session,
            tenant_id=tenant_id,
            ma_agent_id=ma_agent_id,
            account_id=caller_account_id if platform_user_id is None else None,
            platform=platform,
            platform_user_id=platform_user_id,
        ),
    )


__all__ = [
    "AgentReach",
    "build_agent_reach",
    "load_agent_reach",
    "load_target_facts",
    "may_bind_as_channel_default",
]
