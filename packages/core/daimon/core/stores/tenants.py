"""Tenant store — identity, lifecycle, and discovery queries.

Owns tenant identity (create/get/delete), lifecycle helpers, and discovery
queries re-keyed on tenant_id.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from daimon.core._models import (
    AgentFile,
    AgentRepoBinding,
    ChannelConfig,
    PaymentEvent,
    Routine,
    Tenant,
    TenantConfig,
    TenantLedger,
    TenantUserCap,
    UsageEvent,
)
from daimon.core.errors import StoreError
from daimon.core.stores.domain import Platform, TenantDependentCounts, TenantRow
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> TenantRow | None:
    """Return TenantRow for an existing id, None for unknown."""
    row = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if row is None:
        return None
    return TenantRow.model_validate(row)


async def list_all_tenant_ids(session: AsyncSession) -> set[uuid.UUID]:
    """Return every tenant id this deployment owns. Used to scope workspace-wide
    MA sweeps to tenants that actually exist in this DB — a shared MA workspace
    can hold sessions tagged with tenant_ids from other deployments/evals."""
    rows = (await session.execute(select(Tenant.id))).scalars().all()
    return set(rows)


async def get_tenant_liveness(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> TenantRow | None:
    """Bot hot-path wrapper — opens its own session and returns TenantRow | None."""
    async with session_factory() as session:
        return await get_tenant(session, tenant_id)


async def set_provision_status(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    status: str | None = None,
    archive: bool = False,
    clear_archive: bool = False,
) -> None:
    """Update provision_status and/or archived_at for a tenant.

    Raises StoreError when archive and clear_archive are both set (mutually exclusive).
    No-ops when all keyword args are their defaults.
    """
    if archive and clear_archive:
        raise StoreError("archive and clear_archive are mutually exclusive")
    values: dict[str, object] = {}
    if status is not None:
        values["provision_status"] = status
    if archive:
        values["archived_at"] = func.now()
    elif clear_archive:
        values["archived_at"] = None
    if not values:
        return
    async with session_factory() as session, session.begin():
        await session.execute(update(Tenant).where(Tenant.id == tenant_id).values(**values))


async def list_tenants_by_platform(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    platform: Platform | None = None,
) -> list[TenantRow]:
    """Return all tenants ordered by external_id, optionally filtered by platform."""
    async with session_factory() as session:
        stmt = select(Tenant).order_by(Tenant.external_id)
        if platform is not None:
            stmt = stmt.where(Tenant.platform == platform)
        rows = (await session.execute(stmt)).scalars().all()
        return [TenantRow.model_validate(r) for r in rows]


async def delete_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
) -> None:
    """Delete a tenant row; DB ON DELETE CASCADE handles all child tables.

    Raises StoreError when the tenant does not exist.
    """
    stmt = delete(Tenant).where(Tenant.id == tenant_id)
    result = await session.execute(stmt)
    if cast(CursorResult[Any], result).rowcount == 0:
        raise StoreError(f"tenant {tenant_id} not found")
    await session.flush()


async def get_tenant_dependent_counts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
) -> TenantDependentCounts:
    """Return per-table dependent-row counts for a tenant (blast-radius preview)."""
    routines = (
        await session.execute(
            select(func.count()).select_from(Routine).where(Routine.tenant_id == tenant_id)
        )
    ).scalar_one()
    usage_events = (
        await session.execute(
            select(func.count()).select_from(UsageEvent).where(UsageEvent.tenant_id == tenant_id)
        )
    ).scalar_one()
    payment_events = (
        await session.execute(
            select(func.count())
            .select_from(PaymentEvent)
            .where(PaymentEvent.tenant_id == tenant_id)
        )
    ).scalar_one()
    tenant_ledger = (
        await session.execute(
            select(func.count())
            .select_from(TenantLedger)
            .where(TenantLedger.tenant_id == tenant_id)
        )
    ).scalar_one()
    tenant_user_caps = (
        await session.execute(
            select(func.count())
            .select_from(TenantUserCap)
            .where(TenantUserCap.tenant_id == tenant_id)
        )
    ).scalar_one()
    agent_files = (
        await session.execute(
            select(func.count()).select_from(AgentFile).where(AgentFile.tenant_id == tenant_id)
        )
    ).scalar_one()
    agent_repo_binding = (
        await session.execute(
            select(func.count())
            .select_from(AgentRepoBinding)
            .where(AgentRepoBinding.tenant_id == tenant_id)
        )
    ).scalar_one()
    tenant_config = (
        await session.execute(
            select(func.count())
            .select_from(TenantConfig)
            .where(TenantConfig.tenant_id == tenant_id)
        )
    ).scalar_one()
    channel_config = (
        await session.execute(
            select(func.count())
            .select_from(ChannelConfig)
            .where(ChannelConfig.tenant_id == tenant_id)
        )
    ).scalar_one()
    return TenantDependentCounts(
        routines=routines,
        usage_events=usage_events,
        payment_events=payment_events,
        tenant_ledger=tenant_ledger,
        tenant_user_caps=tenant_user_caps,
        agent_files=agent_files,
        agent_repo_binding=agent_repo_binding,
        tenant_config=tenant_config,
        channel_config=channel_config,
    )
