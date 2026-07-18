"""Append-only per-tenant USD ledger store.

Balance = SUM(delta_usd) — NEVER a mutable column. Every money write
is an idempotent INSERT keyed on a natural identity (Stripe event_id, turn id,
trial:{tenant}); on_conflict_do_nothing(idempotency_key) makes replays a no-op.

Per `guideline:architecture`: this module does NOT swallow exceptions.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, cast

from daimon.core._models import TenantLedger
from daimon.core.stores.domain import TenantLedgerRow
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def insert_entry(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    delta_usd: Decimal,
    reason: str,
    idempotency_key: str,
    payment_event_id: str | None = None,
    payment_intent: str | None = None,
) -> bool:
    """INSERT ... ON CONFLICT (idempotency_key) DO NOTHING. True iff a row was inserted."""
    stmt = (
        pg_insert(TenantLedger)
        .values(
            tenant_id=tenant_id,
            delta_usd=delta_usd,
            reason=reason,
            idempotency_key=idempotency_key,
            payment_event_id=payment_event_id,
            payment_intent=payment_intent,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    result = await session.execute(stmt)
    await session.flush()
    return cast(CursorResult[Any], result).rowcount > 0


async def get_balance(session: AsyncSession, *, tenant_id: uuid.UUID) -> Decimal:
    """Balance = SUM(delta_usd). Empty ledger -> Decimal('0'). Negative allowed."""
    stmt = select(func.coalesce(func.sum(TenantLedger.delta_usd), Decimal("0"))).where(
        TenantLedger.tenant_id == tenant_id
    )
    return (await session.execute(stmt)).scalar_one()  # type: ignore[no-any-return]


async def get_clawed_back_total(session: AsyncSession, *, payment_intent: str) -> Decimal:
    """Positive total already clawed back for a payment_intent.

    Sums the negative ledger rows (refund/dispute clawbacks) for the given
    payment_intent and returns the magnitude as a POSITIVE Decimal. The positive
    topup credit row is excluded by filtering on delta_usd < 0 (sign, not reason —
    reason-agnostic like get_balance). No clawback rows -> Decimal('0').
    """
    stmt = select(func.coalesce(-func.sum(TenantLedger.delta_usd), Decimal("0"))).where(
        TenantLedger.payment_intent == payment_intent,
        TenantLedger.delta_usd < Decimal("0"),
    )
    return (await session.execute(stmt)).scalar_one()  # type: ignore[no-any-return]


async def get_by_payment_intent(
    session: AsyncSession, *, payment_intent: str
) -> TenantLedgerRow | None:
    """Find the original topup credit row by its Stripe payment_intent.

    Used by the clawback path (Plan 03) to resolve the tenant + original amount
    for a charge.refunded / charge.dispute.created event, which carries the
    payment_intent but not the original Checkout metadata.
    """
    stmt = (
        select(TenantLedger)
        .where(
            TenantLedger.payment_intent == payment_intent,
            TenantLedger.reason == "topup",
        )
        .limit(1)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    if orm is None:
        return None
    return TenantLedgerRow.model_validate(orm, from_attributes=True)
