"""Per-(tenant, user) cap store. BILL-01.

A row with `platform_user_id IS NULL` is the tenant-wide default. A row with a
non-null platform_user_id is a per-user override. The UNIQUE (tenant_id,
platform_user_id) constraint is `NULLS NOT DISTINCT` (Postgres 15+), so a
second `set_default` on the same tenant upserts the existing default rather
than inserting a duplicate.

`get_effective_cap` returns override > default > None, in that priority.

Per `guideline:architecture` (D-25): this module does NOT swallow exceptions.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, cast

from daimon.core._models import TenantUserCap
from daimon.core.stores.domain import TenantUserCapRow
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def _upsert(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str | None,
    amount: Decimal,
) -> TenantUserCapRow:
    stmt = (
        pg_insert(TenantUserCap)
        .values(
            tenant_id=tenant_id,
            platform_user_id=platform_user_id,
            cap_usd=amount,
        )
        .on_conflict_do_update(
            constraint="uq_tenant_user_caps_tenant_user",
            set_={"cap_usd": amount, "updated_at": func.now()},
        )
        .returning(TenantUserCap)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one()
    await session.flush()
    return TenantUserCapRow.model_validate(orm, from_attributes=True)


async def set_default(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    amount: Decimal,
) -> TenantUserCapRow:
    """Upsert the tenant-wide default (NULL platform_user_id) row."""
    return await _upsert(
        session,
        tenant_id=tenant_id,
        platform_user_id=None,
        amount=amount,
    )


async def set_override(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    amount: Decimal,
) -> TenantUserCapRow:
    """Upsert a per-user override row for `(tenant_id, user_id)`."""
    return await _upsert(
        session,
        tenant_id=tenant_id,
        platform_user_id=user_id,
        amount=amount,
    )


async def clear_override(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
) -> None:
    """Delete the user's override row only — never the default."""
    await session.execute(
        delete(TenantUserCap).where(
            TenantUserCap.tenant_id == tenant_id,
            TenantUserCap.platform_user_id == user_id,
        )
    )
    await session.flush()


async def get_effective_cap(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
) -> Decimal | None:
    """Return override > default > None, in that priority."""
    override = (
        await session.execute(
            select(TenantUserCap.cap_usd).where(
                TenantUserCap.tenant_id == tenant_id,
                TenantUserCap.platform_user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if override is not None:
        return override
    default = (
        await session.execute(
            select(TenantUserCap.cap_usd).where(
                TenantUserCap.tenant_id == tenant_id,
                TenantUserCap.platform_user_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    return default


async def list_overrides(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
) -> list[TenantUserCapRow]:
    """All non-default rows (platform_user_id IS NOT NULL) for the tenant."""
    result = await session.execute(
        select(TenantUserCap).where(
            TenantUserCap.tenant_id == tenant_id,
            TenantUserCap.platform_user_id.is_not(None),
        )
    )
    return [
        TenantUserCapRow.model_validate(r, from_attributes=True) for r in result.scalars().all()
    ]


async def delete_all_for_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
) -> int:
    """Phase 17 GDPR. Deletes ONLY this user's override rows within one tenant.

    The default rows (platform_user_id IS NULL) belong to the tenant, not to
    any user, and are never touched. Postgres `NULL = '<value>'` is NULL (not
    true), so the equality predicate already excludes them.

    `tenant_id` is required: `platform_user_id` is NOT globally unique — Slack
    user ids are workspace-scoped, so `U123` in two workspaces are two different
    humans. Deleting without a tenant filter would erase another tenant's caps.
    """
    result = await session.execute(
        delete(TenantUserCap).where(
            TenantUserCap.tenant_id == tenant_id,
            TenantUserCap.platform_user_id == platform_user_id,
        )
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount
