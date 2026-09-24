"""Durable Stripe clawbacks received before their Checkout credit exists."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from daimon.core._models import PendingPaymentClawback
from daimon.core.stores.domain import PendingPaymentClawbackRow
from sqlalchemy import CursorResult, select, text
from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def lock_payment_intent(session: AsyncSession, *, payment_intent: str) -> None:
    """Serialize completion and clawback transactions for one payment intent.

    A credit row cannot be the common lock target before the first topup exists.
    This transaction-scoped advisory lock covers both that missing-row state
    and the later ledger row. The key is namespaced and hashed in PostgreSQL.
    """
    lock_key = f"daimon:stripe:payment-intent:{payment_intent}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": lock_key},
    )


async def enqueue(
    session: AsyncSession,
    *,
    event_id: str,
    payment_intent: str,
    event_type: str,
    target_amount_usd: Decimal | None,
) -> PendingPaymentClawbackRow:
    """Persist one verified event; re-delivery of the same ID is idempotent."""
    stmt = (
        pg_insert(PendingPaymentClawback)
        .values(
            event_id=event_id,
            payment_intent=payment_intent,
            event_type=event_type,
            target_amount_usd=target_amount_usd,
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
    )
    await session.execute(stmt)
    await session.flush()
    result = await session.execute(
        select(PendingPaymentClawback).where(PendingPaymentClawback.event_id == event_id)
    )
    row = PendingPaymentClawbackRow.model_validate(result.scalar_one(), from_attributes=True)
    if (
        row.payment_intent != payment_intent
        or row.event_type != event_type
        or row.target_amount_usd != target_amount_usd
    ):
        raise ValueError(f"pending Stripe event {event_id!r} changed payload across deliveries")
    return row


async def list_for_payment_intent(
    session: AsyncSession, *, payment_intent: str
) -> list[PendingPaymentClawbackRow]:
    """Return unresolved events in stable insertion order for draining."""
    result = await session.execute(
        select(PendingPaymentClawback)
        .where(PendingPaymentClawback.payment_intent == payment_intent)
        .order_by(PendingPaymentClawback.received_at, PendingPaymentClawback.event_id)
    )
    return [
        PendingPaymentClawbackRow.model_validate(row, from_attributes=True)
        for row in result.scalars().all()
    ]


async def remove(session: AsyncSession, *, event_id: str) -> None:
    """Remove a pending event after its effect or no-op is committed."""
    await session.execute(
        sa_delete(PendingPaymentClawback).where(PendingPaymentClawback.event_id == event_id)
    )


async def expire_before(session: AsyncSession, *, cutoff: datetime) -> int:
    """Delete unmatched events outside the supported Stripe resend window."""
    result = await session.execute(
        sa_delete(PendingPaymentClawback).where(PendingPaymentClawback.received_at < cutoff)
    )
    return int(cast(CursorResult[Any], result).rowcount or 0)
