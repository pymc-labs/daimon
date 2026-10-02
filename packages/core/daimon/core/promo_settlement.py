"""Settle timed promo credit: grant opened windows, expire closed ones, restore late spend.

A closed window's remainder expires at the first tick after it closes,
measured from the spend recorded by then, so the balance stops counting it at
once. Turn debits are dated by the model call and the usage sweep can record
one a tick or more late, so ``LATE_SPEND_GRACE`` after the close a reconcile
pass recomputes the remainder and credits back the late spend the expiry had
also removed, never more than it removed.

Each write is an idempotent ledger row keyed per code and tenant
(``promo:``, ``promo_expiry:``, ``promo_expiry_refund:``) and the
``granted_at``/``expired_at``/``reconciled_at`` guards make a re-run, or a
second scheduler, a no-op. Callers inject ``now``; exceptions propagate
(`guideline:architecture`).
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Collection
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Protocol

import structlog
from daimon.core.promo_credit import grant_promo_credit, unspent_timed_credit
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores import tenant_ledger
from daimon.core.stores.domain import TimedPromoGrantRow
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

# How long after a window closes spend recorded late but dated inside it is
# still paid from the timed credit. Spend recorded later is ordinary spend.
LATE_SPEND_GRACE = timedelta(minutes=15)

_ZERO = Decimal("0")

Settle = Callable[[AsyncSession, TimedPromoGrantRow], Awaitable[tuple[int, Decimal]]]


class Lock(Protocol):
    def __call__(
        self, session: AsyncSession, /, *, exclude: Collection[uuid.UUID]
    ) -> Awaitable[list[TimedPromoGrantRow]]: ...


@dataclasses.dataclass(frozen=True)
class PromoSettlement:
    granted: int
    expired: int
    expired_usd: Decimal
    restored: int = 0
    restored_usd: Decimal = _ZERO


async def _unspent_at_close(session: AsyncSession, row: TimedPromoGrantRow) -> Decimal:
    unspent = await unspent_timed_credit(
        session, tenant_id=row.tenant_id, targets={row.promo_code_id}, horizon=row.credit_ends_at
    )
    return unspent.get(row.promo_code_id, row.amount_usd)


async def _settle_grant(
    session: AsyncSession, row: TimedPromoGrantRow, *, now: datetime
) -> tuple[int, Decimal]:
    """Grant one due redemption, or close it unfunded if its window already ended."""
    if row.credit_ends_at <= now:
        await promo_store.mark_expired(
            session, redemption_id=row.redemption_id, expired_at=now, expired_usd=_ZERO
        )
        return 0, _ZERO
    await grant_promo_credit(
        session, promo_code_id=row.promo_code_id, tenant_id=row.tenant_id, amount_usd=row.amount_usd
    )
    await promo_store.mark_granted(session, redemption_id=row.redemption_id, granted_at=now)
    return 1, row.amount_usd


async def _settle_expiry(
    session: AsyncSession, row: TimedPromoGrantRow, *, now: datetime
) -> tuple[int, Decimal]:
    """Remove what is left of one closed window, from the spend recorded so far."""
    remaining = await _unspent_at_close(session, row)
    if remaining > 0:
        await tenant_ledger.insert_entry(
            session,
            tenant_id=row.tenant_id,
            delta_usd=-remaining,
            reason="promo_expiry",
            idempotency_key=f"promo_expiry:{row.promo_code_id}:{row.tenant_id}",
        )
    await promo_store.mark_expired(
        session, redemption_id=row.redemption_id, expired_at=now, expired_usd=remaining
    )
    return 1, remaining


async def _settle_reconcile(
    session: AsyncSession, row: TimedPromoGrantRow, *, now: datetime
) -> tuple[int, Decimal]:
    """Credit back late-recorded spend inside a closed window that its expiry also removed."""
    expired = row.expired_usd or _ZERO
    restored = min(expired, max(_ZERO, expired - await _unspent_at_close(session, row)))
    if restored > 0:
        await tenant_ledger.insert_entry(
            session,
            tenant_id=row.tenant_id,
            delta_usd=restored,
            reason="promo_expiry_refund",
            idempotency_key=f"promo_expiry_refund:{row.promo_code_id}:{row.tenant_id}",
        )
    await promo_store.mark_reconciled(session, redemption_id=row.redemption_id, reconciled_at=now)
    return int(restored > 0), restored


def _skip_bad_row(phase: str, row: TimedPromoGrantRow, exc: DBAPIError) -> None:
    """Log a row the database rejected, or re-raise when the connection itself failed."""
    if exc.connection_invalidated or isinstance(exc, OperationalError | InterfaceError):
        raise exc
    log.error(
        "promo_credit.settle_row_failed",
        phase=phase,
        redemption_id=str(row.redemption_id),
        promo_code_id=str(row.promo_code_id),
        tenant_id=str(row.tenant_id),
        exc_info=exc,
    )


async def _settle_in_batches(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    phase: str,
    lock: Lock,
    settle: Settle,
    limit: int,
) -> tuple[int, Decimal]:
    """Settle locked rows, ``limit`` per transaction, until a batch comes back short.

    Each row runs in its own savepoint, so one the database rejects is logged,
    left due and passed over by later batches.
    """
    count, total = 0, _ZERO
    skipped: set[uuid.UUID] = set()
    while True:
        async with session_factory() as session, session.begin():
            rows = await lock(session, exclude=skipped)
            for row in rows:
                try:
                    async with session.begin_nested():
                        settled, amount = await settle(session, row)
                    count, total = count + settled, total + amount
                except DBAPIError as exc:
                    _skip_bad_row(phase, row, exc)
                    skipped.add(row.redemption_id)
        if len(rows) < limit:
            return count, total


async def settle_promo_credit(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime, limit: int = 100
) -> PromoSettlement:
    """Grant windows that opened, expire those that closed, restore their late spend.

    A window that opened and closed while nothing ran is closed without a
    grant. Every phase runs even when an earlier one fails; the first failure
    is raised afterwards.
    """
    phases: list[tuple[str, Lock, Settle]] = [
        (
            "grant",
            partial(promo_store.lock_due_grants, now=now, limit=limit),
            partial(_settle_grant, now=now),
        ),
        (
            "expiry",
            partial(promo_store.lock_due_expiries, closed_by=now, limit=limit),
            partial(_settle_expiry, now=now),
        ),
        (
            "reconcile",
            partial(promo_store.lock_due_reconciles, closed_by=now - LATE_SPEND_GRACE, limit=limit),
            partial(_settle_reconcile, now=now),
        ),
    ]
    results: list[tuple[int, Decimal]] = []
    error: Exception | None = None
    for phase, lock, settle in phases:
        try:
            results.append(
                await _settle_in_batches(
                    session_factory, phase=phase, lock=lock, settle=settle, limit=limit
                )
            )
        except Exception as exc:  # named boundary: one phase's failure must not skip the rest
            log.error("promo_credit.settle_phase_failed", phase=phase, exc_info=exc)
            error = error or exc
            results.append((0, _ZERO))
    (granted, _), (expired, expired_usd), (restored, restored_usd) = results
    if granted or expired or restored:
        log.info(
            "promo_credit.settled",
            granted=granted,
            expired=expired,
            expired_usd=str(expired_usd),
            restored=restored,
            restored_usd=str(restored_usd),
        )
    if error is not None:
        raise error
    return PromoSettlement(granted, expired, expired_usd, restored, restored_usd)
