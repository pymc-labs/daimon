"""Stripe webhook dedup store. BILL-01.

NOT a ledger — D-18. Holds (stripe event_id, amount, credited_at, source,
tenant_id). The PK is the Stripe event id (text), not a surrogate UUID, so
the compare-and-set in `try_claim_credit` reads naturally.

The credit step is a no-op in v1.1 (D-20); the hooks ship for future use.

Per `guideline:architecture` (D-25): this module does NOT swallow exceptions.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, cast

from daimon.core._models import PaymentEvent
from daimon.core.stores.domain import PaymentEventRow
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_for_dedup(
    session: AsyncSession,
    *,
    event_id: str,
    amount_usd: Decimal,
    source: str,
    tenant_id: uuid.UUID,
) -> PaymentEventRow:
    """INSERT ... ON CONFLICT DO NOTHING; SELECT.

    On replay (same event_id), the existing row is returned unchanged — the
    incoming `amount_usd` and `source` are NOT applied. This is the dedup
    contract: Stripe re-delivers events; the first write wins.

    tenant_id is NOT NULL (0016 migration flip) and must be supplied by all callers.
    """
    stmt = (
        pg_insert(PaymentEvent)
        .values(
            id=event_id,
            amount_usd=amount_usd,
            source=source,
            tenant_id=tenant_id,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await session.execute(stmt)
    await session.flush()
    result = await session.execute(select(PaymentEvent).where(PaymentEvent.id == event_id))
    orm = result.scalar_one()
    return PaymentEventRow.model_validate(orm, from_attributes=True)


async def try_claim_credit(session: AsyncSession, event_id: str) -> bool:
    """Compare-and-set on credited_at IS NULL → now(). True if we won."""
    result = await session.execute(
        update(PaymentEvent)
        .where(
            PaymentEvent.id == event_id,
            PaymentEvent.credited_at.is_(None),
        )
        .values(credited_at=func.now())
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount > 0


async def unclaim_credit(session: AsyncSession, event_id: str) -> None:
    """Reset credited_at to NULL. Dead code in v1.1; ships for future use (D-20)."""
    await session.execute(
        update(PaymentEvent).where(PaymentEvent.id == event_id).values(credited_at=None)
    )
    await session.flush()


async def get(session: AsyncSession, event_id: str) -> PaymentEventRow | None:
    """Return the row by event_id, or None if missing."""
    result = await session.execute(select(PaymentEvent).where(PaymentEvent.id == event_id))
    orm = result.scalar_one_or_none()
    if orm is None:
        return None
    return PaymentEventRow.model_validate(orm, from_attributes=True)
