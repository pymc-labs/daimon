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
from daimon.core.stores.domain import FundingMode, Platform, TenantDependentCounts, TenantRow
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


_LAST_RECONCILE_ERROR_MAX_LENGTH = 2000
_LAST_RECONCILE_ERROR_TRUNCATION_MARKER = "... [truncated]"


def _truncate_reason(reason: str) -> str:
    """Cap a reconcile-failure reason so a pathological provider error body
    cannot bloat the tenants table. Appends a marker when truncated."""
    if len(reason) <= _LAST_RECONCILE_ERROR_MAX_LENGTH:
        return reason
    cutoff = _LAST_RECONCILE_ERROR_MAX_LENGTH - len(_LAST_RECONCILE_ERROR_TRUNCATION_MARKER)
    return reason[:cutoff] + _LAST_RECONCILE_ERROR_TRUNCATION_MARKER


async def set_provision_status(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    status: str | None = None,
    archive: bool = False,
    clear_archive: bool = False,
    reason: str | None = None,
    clear_reason: bool = False,
) -> None:
    """Update provision_status, archived_at, and/or last_reconcile_error for a tenant.

    Raises StoreError when archive and clear_archive are both set, or when
    reason and clear_reason are both set (each pair is mutually exclusive).
    No-ops when all keyword args are their defaults.

    A caller flipping a tenant back to `status="ready"` after a successful
    reconcile must pass `clear_reason=True` explicitly — this store does not
    infer clearing from the status value, so the semantics live in exactly one
    place (the caller), not split between here and there. Leaving a reason
    behind a successful reconcile would let a stale failure be read as current.

    `reason` is capped at `_LAST_RECONCILE_ERROR_MAX_LENGTH` characters (with a
    truncation marker appended) before being written, so an unbounded provider
    error body cannot bloat the row.
    """
    if archive and clear_archive:
        raise StoreError("archive and clear_archive are mutually exclusive")
    if reason is not None and clear_reason:
        raise StoreError("reason and clear_reason are mutually exclusive")
    values: dict[str, object] = {}
    if status is not None:
        values["provision_status"] = status
    if archive:
        values["archived_at"] = func.now()
    elif clear_archive:
        values["archived_at"] = None
    if reason is not None:
        values["last_reconcile_error"] = _truncate_reason(reason)
    elif clear_reason:
        values["last_reconcile_error"] = None
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


async def set_funding_mode(
    session: AsyncSession, *, tenant_id: uuid.UUID, funding_mode: FundingMode
) -> TenantRow:
    """Set one tenant's funding policy without changing its ledger or caps."""
    if funding_mode not in ("prepaid", "operator_funded"):
        raise StoreError("funding_mode must be prepaid or operator_funded")
    row = (
        await session.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(funding_mode=funding_mode)
            .returning(Tenant)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise StoreError(f"tenant {tenant_id} not found")
    await session.flush()
    return TenantRow.model_validate(row)
