"""Redeem promo codes, settle timed credit windows, and report live timed credit.

Every money write is an idempotent ledger row: a grant is keyed
``promo:{code_id}:{tenant_id}`` and an expiry ``promo_expiry:{code_id}:{tenant_id}``,
so a retried settlement never double-writes. The balance stays ``SUM(delta_usd)``
and the balance and cap gates never look at promo state.

Callers inject ``now``; exceptions propagate (`guideline:architecture`).
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Collection
from datetime import datetime, timedelta
from decimal import Decimal

import structlog
from daimon.core.promo_allocation import (
    TimedGrant,
    relevant_grants,
    remaining_timed_credit,
    spend_bounds,
)
from daimon.core.promo_codes import (
    PromoRefusal,
    hash_promo_code,
    is_granted_on_redeem,
    is_well_formed_promo_code,
    normalize_promo_code,
    redeem_refusal,
)
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores import tenant_ledger
from daimon.core.stores.domain import PromoCodeKind, TimedPromoGrantRow
from daimon.core.usage_recording import SPEND_LEDGER_REASONS
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

# Refused attempts a tenant may make per window before redemption pauses.
# Generated codes carry 100 bits, so this is defence in depth for short
# operator-chosen codes rather than the only thing standing in the way.
REDEEM_FAILURE_LIMIT = 5
REDEEM_FAILURE_WINDOW = timedelta(minutes=15)


@dataclasses.dataclass(frozen=True)
class PromoRedeemed:
    promo_code_id: uuid.UUID
    kind: PromoCodeKind
    amount_usd: Decimal
    credit_starts_at: datetime | None
    credit_ends_at: datetime | None
    granted: bool  # False: a timed code whose credit starts later
    balance_usd: Decimal


@dataclasses.dataclass(frozen=True)
class PromoRedeemRefused:
    reason: PromoRefusal


PromoRedeemResult = PromoRedeemed | PromoRedeemRefused


@dataclasses.dataclass(frozen=True)
class ActiveTimedCredit:
    remaining_usd: Decimal
    ends_at: datetime


@dataclasses.dataclass(frozen=True)
class PromoSettlement:
    granted: int
    expired: int
    expired_usd: Decimal


async def _grant(
    session: AsyncSession, *, promo_code_id: uuid.UUID, tenant_id: uuid.UUID, amount_usd: Decimal
) -> None:
    await tenant_ledger.insert_entry(
        session,
        tenant_id=tenant_id,
        delta_usd=amount_usd,
        reason="promo_credit",
        idempotency_key=f"promo:{promo_code_id}:{tenant_id}",
    )


async def redeem_promo_code(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    code: str,
    now: datetime,
) -> PromoRedeemResult:
    """Redeem ``code`` for the tenant in one transaction; a refusal is counted for throttling.

    The code row is locked first, so the per-code limit and the one-per-tenant
    rule are checked and applied serially. The throttle count itself is not
    locked: concurrent guesses can overshoot the limit by a few, which is fine
    for a guessing brake.
    """
    since = now - REDEEM_FAILURE_WINDOW
    async with session_factory() as session, session.begin():
        failures = await promo_store.count_redeem_failures(
            session, tenant_id=tenant_id, since=since
        )
        if failures >= REDEEM_FAILURE_LIMIT:
            log.warning("promo_code.redeem_throttled", tenant_id=str(tenant_id))
            return PromoRedeemRefused(reason="throttled")
        result = await _redeem_in_session(
            session, tenant_id=tenant_id, account_id=account_id, code=code, now=now
        )
        if isinstance(result, PromoRedeemRefused):
            await promo_store.record_redeem_failure(
                session, tenant_id=tenant_id, now=now, prune_before=since
            )
            log.info("promo_code.redeem_refused", tenant_id=str(tenant_id), reason=result.reason)
        else:
            log.info(
                "promo_code.redeemed",
                tenant_id=str(tenant_id),
                promo_code_id=str(result.promo_code_id),
                kind=result.kind,
                granted=result.granted,
            )
        return result


async def _redeem_in_session(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    code: str,
    now: datetime,
) -> PromoRedeemResult:
    normalized = normalize_promo_code(code)
    if not is_well_formed_promo_code(normalized):
        return PromoRedeemRefused(reason="invalid")
    row = await promo_store.lock_promo_code_by_hash(session, hash_promo_code(normalized))
    if row is None:
        return PromoRedeemRefused(reason="invalid")
    if await promo_store.has_redemption(session, promo_code_id=row.id, tenant_id=tenant_id):
        return PromoRedeemRefused(reason="already_redeemed")
    refusal = redeem_refusal(row, now=now)
    if refusal is not None:
        return PromoRedeemRefused(reason=refusal)
    granted = is_granted_on_redeem(row, now=now)
    if not await promo_store.insert_redemption(
        session,
        promo_code_id=row.id,
        tenant_id=tenant_id,
        account_id=account_id,
        now=now,
        granted=granted,
    ):
        return PromoRedeemRefused(reason="already_redeemed")
    if granted:
        await _grant(session, promo_code_id=row.id, tenant_id=tenant_id, amount_usd=row.amount_usd)
    return PromoRedeemed(
        promo_code_id=row.id,
        kind=row.kind,
        amount_usd=row.amount_usd,
        credit_starts_at=row.credit_starts_at,
        credit_ends_at=row.credit_ends_at,
        granted=granted,
        balance_usd=await tenant_ledger.get_balance(session, tenant_id=tenant_id),
    )


def _timed_grant(row: TimedPromoGrantRow) -> TimedGrant | None:
    if row.granted_at is None:
        return None
    return TimedGrant(
        promo_code_id=row.promo_code_id,
        amount_usd=row.amount_usd,
        starts_at=row.granted_at,
        ends_at=row.credit_ends_at,
    )


async def _remaining_at(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    targets: Collection[uuid.UUID],
    horizon: datetime,
) -> dict[uuid.UUID, Decimal]:
    """Unspent timed credit per code at ``horizon``, from the tenant's spend history."""
    rows = await promo_store.list_timed_grants(session, tenant_id=tenant_id)
    grants = [grant for row in rows if (grant := _timed_grant(row)) is not None]
    grants = relevant_grants(grants, targets=targets, horizon=horizon)
    bounds = spend_bounds(grants, horizon=horizon)
    spend = await tenant_ledger.get_spend_by_interval(
        session, tenant_id=tenant_id, bounds=bounds, reasons=SPEND_LEDGER_REASONS
    )
    return remaining_timed_credit(grants, bounds=bounds, spend=spend)


async def get_active_timed_credit(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime
) -> list[ActiveTimedCredit]:
    """Timed credit live at ``now`` with what is left of it, soonest-ending first."""
    rows = await promo_store.list_timed_grants(session, tenant_id=tenant_id)
    live = {r.promo_code_id: r for r in rows if r.expired_at is None and r.credit_ends_at > now}
    if not live:
        return []
    remaining = await _remaining_at(session, tenant_id=tenant_id, targets=live, horizon=now)
    credits = [
        ActiveTimedCredit(
            remaining_usd=remaining.get(code_id, row.amount_usd), ends_at=row.credit_ends_at
        )
        for code_id, row in live.items()
    ]
    return sorted(credits, key=lambda credit: credit.ends_at)


async def settle_promo_credit(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime, limit: int = 100
) -> PromoSettlement:
    """Grant timed credit whose window opened and expire what is left where it closed.

    Idempotent: the ledger keys and the ``granted_at``/``expired_at`` guards make
    a re-run, or a second scheduler, a no-op. A window that opened and closed
    while nothing ran is marked expired without ever granting.
    """
    granted = 0
    async with session_factory() as session, session.begin():
        for row in await promo_store.lock_due_grants(session, now=now, limit=limit):
            if row.credit_ends_at <= now:
                await promo_store.mark_expired(
                    session,
                    redemption_id=row.redemption_id,
                    expired_at=now,
                    expired_usd=Decimal("0"),
                )
                continue
            await _grant(
                session,
                promo_code_id=row.promo_code_id,
                tenant_id=row.tenant_id,
                amount_usd=row.amount_usd,
            )
            await promo_store.mark_granted(session, redemption_id=row.redemption_id, granted_at=now)
            granted += 1
    expired = 0
    expired_usd = Decimal("0")
    async with session_factory() as session, session.begin():
        for row in await promo_store.lock_due_expiries(session, now=now, limit=limit):
            remaining = (
                await _remaining_at(
                    session,
                    tenant_id=row.tenant_id,
                    targets={row.promo_code_id},
                    horizon=row.credit_ends_at,
                )
            ).get(row.promo_code_id, row.amount_usd)
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
            expired += 1
            expired_usd += remaining
    if granted or expired:
        log.info(
            "promo_credit.settled",
            granted=granted,
            expired=expired,
            expired_usd=str(expired_usd),
        )
    return PromoSettlement(granted=granted, expired=expired, expired_usd=expired_usd)
