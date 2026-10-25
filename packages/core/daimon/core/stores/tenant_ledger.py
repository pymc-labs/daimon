"""Append-only per-tenant USD ledger store.

Balance = SUM(delta_usd) — NEVER a mutable column. Every money write
is an idempotent INSERT keyed on a natural identity (Stripe event_id, turn id,
trial:{tenant}); on_conflict_do_nothing(idempotency_key) makes replays a no-op.

Per `guideline:architecture`: this module does NOT swallow exceptions.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from daimon.core._models import Tenant, TenantLedger
from daimon.core.stores import read_persisted_columns
from daimon.core.stores.domain import LedgerEstimate, TenantLedgerRow
from sqlalchemy import DateTime, Select, and_, func, or_, select
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

#: session.info key: the transaction in which this session inserted a ledger row.
_LEDGER_SHARE_TAKEN = "tenant_ledger_share_taken"


async def lock_funds(
    session: AsyncSession, *, tenant_id: uuid.UUID, financial: bool = True
) -> uuid.UUID | None:
    """Fence one tenant's append-only funds inside the caller's transaction.

    A funds check takes FOR NO KEY UPDATE on the tenant row: exclusive against every
    other funds check, and against every ledger insert, which takes FOR SHARE on the
    same row first (`insert_entry`). It waits for in-flight inserts and fences later
    ones until commit, without blocking another tenant's ledger.

    It deliberately no longer conflicts with the foreign-key KEY SHARE every OTHER
    tenant-scoped insert takes (journal, operations, tokens, outcomes...): FOR UPDATE
    did, so every write in a busy tenant queued behind each funds check (bind
    latency, lockwatch a1d991fb). The ledger is the only money table a funds check
    reads, and its one writer takes the explicit share lock.

    Passive refresh takes KEY SHARE instead, protecting tenant lifetime without
    fencing ordinary writes. Acquire before actor and reservation rows, and before
    any ledger insert in the same transaction (SHARE then NO KEY UPDATE would be an
    upgrade two such transactions deadlock on; refused below). Never hold over native
    I/O.
    """
    if financial and session.info.get(_LEDGER_SHARE_TAKEN) is session.get_transaction():
        raise RuntimeError(
            "lock a tenant's funds before inserting into its ledger in one transaction"
        )
    statement = select(Tenant.id).where(Tenant.id == tenant_id)
    statement = (
        statement.with_for_update(key_share=True)
        if financial
        else statement.with_for_update(read=True, key_share=True)
    )
    return await session.scalar(statement)


async def insert_entry(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    delta_usd: Decimal,
    reason: str,
    idempotency_key: str,
    payment_event_id: str | None = None,
    payment_intent: str | None = None,
    channel_id: str | None = None,
    occurred_at: datetime | None = None,
    estimate: LedgerEstimate | None = None,
) -> bool:
    """INSERT ... ON CONFLICT (idempotency_key) DO NOTHING. True iff a row was inserted.

    `channel_id` is set on debits only: the channel whose budget the spend counts against.
    `estimate` marks a debit priced from incomplete provider counts.
    ``occurred_at`` dates the entry by when it happened (a model call's own
    time) rather than when it was written; the database clock is the default.
    """
    # The money fence: a funds check (lock_funds, FOR NO KEY UPDATE) excludes this
    # share lock, so a ledger row never lands between a funds check and its decision.
    # The foreign key's own KEY SHARE no longer does that job.
    await session.execute(
        select(Tenant.id).where(Tenant.id == tenant_id).with_for_update(read=True)
    )
    session.info[_LEDGER_SHARE_TAKEN] = session.get_transaction()
    dated = {} if occurred_at is None else {"occurred_at": occurred_at}
    stmt = (
        pg_insert(TenantLedger)
        .values(
            tenant_id=tenant_id,
            delta_usd=delta_usd,
            reason=reason,
            idempotency_key=idempotency_key,
            payment_event_id=payment_event_id,
            payment_intent=payment_intent,
            channel_id=channel_id,
            estimate=estimate,
            **dated,
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


async def ever_credited(session: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    """Whether any credit (signup, top-up or grant) was ever added to this tenant."""
    stmt = select(
        select(TenantLedger.id)
        .where(TenantLedger.tenant_id == tenant_id, TenantLedger.delta_usd > 0)
        .exists()
    )
    return bool((await session.execute(stmt)).scalar_one())


async def get_channel_spend(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    since: datetime | None,
    until: datetime | None,
    persisted: bool = False,
) -> Decimal:
    """Positive USD debited to a channel in `[since, until)`; an open end is unbounded.

    Reads the ledger, so markup is included and a model call counts once
    whichever writer (live recorder or sweep) recorded it.
    """
    stmt = select(func.coalesce(-func.sum(TenantLedger.delta_usd), Decimal("0"))).where(
        TenantLedger.tenant_id == tenant_id,
        TenantLedger.channel_id == channel_id,
        or_(
            TenantLedger.delta_usd < Decimal("0"),
            and_(
                TenantLedger.reason.in_(("turn_debit", "checkpoint_debit")),
                TenantLedger.idempotency_key.like("adjust:%"),
            ),
        ),
    )
    if since is not None:
        stmt = stmt.where(TenantLedger.occurred_at >= since)
    if until is not None:
        stmt = stmt.where(TenantLedger.occurred_at < until)
    result = (
        await read_persisted_columns(session, stmt, reject_new=True)
        if persisted
        else await session.execute(stmt)
    )
    return result.scalar_one()  # type: ignore[no-any-return]


def _prepaid_balance_query(tenant_id: uuid.UUID) -> Select[tuple[Decimal]]:
    return (
        select(func.coalesce(func.sum(TenantLedger.delta_usd), Decimal("0")))
        .select_from(Tenant)
        .outerjoin(TenantLedger, TenantLedger.tenant_id == Tenant.id)
        .where(Tenant.id == tenant_id, Tenant.funding_mode == "prepaid")
        .group_by(Tenant.id)
    )


async def get_prepaid_balance(
    session: AsyncSession, *, tenant_id: uuid.UUID, persisted: bool = False
) -> Decimal | None:
    """Return the current balance for a prepaid tenant in one query."""
    stmt = _prepaid_balance_query(tenant_id)
    result = (
        await read_persisted_columns(session, stmt, reject_new=True)
        if persisted
        else await session.execute(stmt)
    )
    return result.scalar_one_or_none()


async def get_spend_by_interval(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    bounds: Sequence[datetime],
    reasons: Sequence[str],
) -> list[Decimal]:
    """Positive spend per interval ``[bounds[i], bounds[i+1])``, one scan of the tenant's rows.

    ``bounds`` must be sorted. Only rows whose reason is in ``reasons`` count.
    """
    if len(bounds) < 2:
        return []
    thresholds = pg_array(list(bounds), type_=DateTime(timezone=True))
    bucket = func.width_bucket(TenantLedger.occurred_at, thresholds).label("bucket")
    stmt = (
        select(bucket, func.sum(-TenantLedger.delta_usd))
        .where(
            TenantLedger.tenant_id == tenant_id,
            TenantLedger.reason.in_(reasons),
            TenantLedger.occurred_at >= bounds[0],
            TenantLedger.occurred_at < bounds[-1],
        )
        .group_by(bucket)
    )
    spend = [Decimal("0")] * (len(bounds) - 1)
    for index, total in (await session.execute(stmt)).tuples():
        spend[int(index) - 1] = Decimal(total)
    return spend


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


async def list_for_tenant(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> Sequence[TenantLedgerRow]:
    """List every ledger row for a tenant (e.g. test assertions on individual entries)."""
    stmt = select(TenantLedger).where(TenantLedger.tenant_id == tenant_id)
    result = await session.execute(stmt)
    return [TenantLedgerRow.model_validate(r, from_attributes=True) for r in result.scalars().all()]


async def get_by_payment_intent(
    session: AsyncSession, *, payment_intent: str, for_update: bool = False
) -> TenantLedgerRow | None:
    """Find the original topup credit row by its Stripe payment_intent.

    Used by the clawback path to resolve the tenant + original amount
    for a charge.refunded / charge.dispute.created event, which carries the
    payment_intent but not the original Checkout metadata.
    Lock the credit row while calculating a clawback to serialize distinct events.
    """
    stmt = (
        select(TenantLedger)
        .where(
            TenantLedger.payment_intent == payment_intent,
            TenantLedger.reason == "topup",
        )
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    orm = (await session.execute(stmt)).scalar_one_or_none()
    if orm is None:
        return None
    return TenantLedgerRow.model_validate(orm, from_attributes=True)
