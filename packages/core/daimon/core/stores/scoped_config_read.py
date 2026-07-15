"""Centralized config reads: three-tier resolution and single-scope raw reads."""

from __future__ import annotations

import uuid
from typing import Literal, cast

from daimon.core._models import ChannelConfig, TenantConfig, UserConfig
from daimon.core.scope import (
    ChannelConfigRow,
    ChannelScopeRef,
    DeploymentDefault,
    ResolvedConfig,
    ScopeContext,
    ScopeRef,
    TenantConfigRow,
    UserConfigRow,
    UserScopeRef,
    merge,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def resolve(
    session: AsyncSession,
    *,
    context: ScopeContext,
    default: DeploymentDefault,
) -> ResolvedConfig:
    channel_row: ChannelConfigRow | None = None
    if context.channel_id is not None:
        channel_row = await _fetch_channel(
            session,
            tenant_id=context.tenant_id,
            channel_id=context.channel_id,
        )

    tenant_row = await _fetch_tenant(session, tenant_id=context.tenant_id)
    return merge(channel=channel_row, tenant=tenant_row, default=default)


async def get_scope(
    session: AsyncSession, *, scope: ScopeRef
) -> UserConfigRow | ChannelConfigRow | TenantConfigRow | None:
    if isinstance(scope, UserScopeRef):
        return await _fetch_user(session, account_id=scope.account_id)
    if isinstance(scope, ChannelScopeRef):
        return await _fetch_channel(
            session,
            tenant_id=scope.tenant_id,
            channel_id=scope.channel_id,
        )
    # TenantScopeRef is the only remaining variant.
    return await _fetch_tenant(session, tenant_id=scope.tenant_id)


async def list_propagations_for_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
) -> tuple[TenantConfigRow | None, list[ChannelConfigRow]]:
    """Return the tenant-scope row (if any) and all channel-scope rows for one tenant.

    Maps ORM -> Pydantic at the boundary. Used by the Discord /propagate +
    /unpropagate panels to render the cross-channel cascade. Lives inside the
    store because the ORM models (`ChannelConfig`, `TenantConfig`) are private to
    `daimon.core.stores.**` per the import-linter ORM-privacy contract — adapters
    consume only Pydantic rows.
    """
    tenant_row = await _fetch_tenant(session, tenant_id=tenant_id)

    ch_stmt = (
        select(ChannelConfig)
        .where(ChannelConfig.tenant_id == tenant_id)
        .order_by(ChannelConfig.channel_id.asc())
    )
    ch_orms = (await session.execute(ch_stmt)).scalars().all()
    ch_rows = [
        ChannelConfigRow(
            tenant_id=ch.tenant_id,
            channel_id=ch.channel_id,
            agent_name=ch.agent_name,
            environment_name=ch.environment_name,
            agent_name_set_by_account_id=ch.agent_name_set_by_account_id,
            agent_name_set_at=ch.agent_name_set_at,
            mode=cast(Literal["agent", "user_active"], ch.mode),
        )
        for ch in ch_orms
    ]
    return tenant_row, ch_rows


async def _fetch_user(session: AsyncSession, *, account_id: uuid.UUID) -> UserConfigRow | None:
    stmt = select(UserConfig).where(UserConfig.account_id == account_id)
    orm = (await session.execute(stmt)).scalar_one_or_none()
    if orm is None:
        return None
    return UserConfigRow(agent_name=orm.agent_name, environment_name=orm.environment_name)


async def _fetch_channel(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
) -> ChannelConfigRow | None:
    stmt = select(ChannelConfig).where(
        ChannelConfig.tenant_id == tenant_id,
        ChannelConfig.channel_id == channel_id,
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    if orm is None:
        return None
    return ChannelConfigRow(
        tenant_id=orm.tenant_id,
        channel_id=orm.channel_id,
        agent_name=orm.agent_name,
        environment_name=orm.environment_name,
        agent_name_set_by_account_id=orm.agent_name_set_by_account_id,
        agent_name_set_at=orm.agent_name_set_at,
        mode=cast(Literal["agent", "user_active"], orm.mode),
    )


async def _fetch_tenant(session: AsyncSession, *, tenant_id: uuid.UUID) -> TenantConfigRow | None:
    stmt = select(TenantConfig).where(TenantConfig.tenant_id == tenant_id)
    orm = (await session.execute(stmt)).scalar_one_or_none()
    if orm is None:
        return None
    return TenantConfigRow(
        tenant_id=orm.tenant_id,
        agent_name=orm.agent_name,
        environment_name=orm.environment_name,
        mode=cast(Literal["agent", "user_active"], orm.mode),
        agent_name_set_by_account_id=orm.agent_name_set_by_account_id,
        agent_name_set_at=orm.agent_name_set_at,
    )
