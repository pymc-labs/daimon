"""Spend allowances: per-person limits in billed dollars. Pure rules live in
`daimon.core.allowances`; this module stores them and counts what a person has used.

Per `guideline:architecture`, exceptions propagate.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime
from decimal import Decimal

from daimon.core._models import SpendAllowance
from daimon.core.stores import usage_events
from daimon.core.stores.domain import AllowanceSubjectKind, AllowanceWindow, SpendAllowanceRow
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def list_for_tenant(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[SpendAllowanceRow]:
    """Every allowance of a tenant, oldest first."""
    rows = await session.scalars(
        select(SpendAllowance)
        .where(SpendAllowance.tenant_id == tenant_id)
        .order_by(SpendAllowance.created_at, SpendAllowance.id)
    )
    return [SpendAllowanceRow.model_validate(row) for row in rows]


async def create_allowance(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject_kind: AllowanceSubjectKind,
    subject_id: str,
    agent_id: str | None,
    amount_usd: Decimal,
    window: AllowanceWindow,
    starts_at: datetime,
    expires_at: datetime | None = None,
    created_by_account_id: uuid.UUID | None = None,
    is_cap: bool = False,
) -> SpendAllowanceRow:
    """Add an allowance (a cap when `is_cap`) and return it."""
    orm = SpendAllowance(
        tenant_id=tenant_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
        agent_id=agent_id,
        amount_usd=amount_usd,
        window=window,
        starts_at=starts_at,
        expires_at=expires_at,
        created_by_account_id=created_by_account_id,
        is_cap=is_cap,
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return SpendAllowanceRow.model_validate(orm)


async def find_month_cap(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject_kind: AllowanceSubjectKind,
    subject_id: str,
) -> SpendAllowanceRow | None:
    """The subject's monthly cap, if it has one."""
    orm = await session.scalar(
        select(SpendAllowance)
        .where(
            SpendAllowance.tenant_id == tenant_id,
            SpendAllowance.subject_kind == subject_kind,
            SpendAllowance.subject_id == subject_id,
            SpendAllowance.is_cap.is_(True),
            SpendAllowance.agent_id.is_(None),
            SpendAllowance.window == "month",
            SpendAllowance.expires_at.is_(None),
        )
        .order_by(SpendAllowance.created_at, SpendAllowance.id)
        .limit(1)
    )
    return None if orm is None else SpendAllowanceRow.model_validate(orm)


async def set_amount(
    session: AsyncSession, *, tenant_id: uuid.UUID, allowance_id: uuid.UUID, amount_usd: Decimal
) -> SpendAllowanceRow | None:
    """Change what one of the tenant's allowances lets a person spend. None when gone."""
    orm = await session.scalar(
        update(SpendAllowance)
        .where(SpendAllowance.tenant_id == tenant_id, SpendAllowance.id == allowance_id)
        .values(amount_usd=amount_usd)
        .returning(SpendAllowance)
    )
    return None if orm is None else SpendAllowanceRow.model_validate(orm)


async def delete_allowance(
    session: AsyncSession, *, tenant_id: uuid.UUID, allowance_id: uuid.UUID
) -> bool:
    """Remove one of the tenant's allowances. False when it was not there."""
    deleted = await session.scalar(
        delete(SpendAllowance)
        .where(SpendAllowance.tenant_id == tenant_id, SpendAllowance.id == allowance_id)
        .returning(SpendAllowance.id)
    )
    return deleted is not None


async def used_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_ids: Collection[str],
    agent_id: str | None,
    since: datetime,
    markup: Decimal,
) -> Decimal:
    """What the person has spent since `since` under any of `platform_user_ids`, in billed
    dollars (cost times `markup`).

    `agent_id` counts only that agent's spend; None counts every agent. Billed dollars
    are what the ledger debits, so an allowance and the balance agree.
    """
    cost = await usage_events.cost_for_users_in_tenant_since(
        session,
        tenant_id=tenant_id,
        platform_user_ids=platform_user_ids,
        since=since,
        agent_id=agent_id,
    )
    return (Decimal(str(cost)) * markup).quantize(Decimal("0.000001"))
