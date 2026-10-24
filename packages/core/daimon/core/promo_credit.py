"""Redeem promo codes and report live timed credit.

A server-wide grant is an idempotent ledger row keyed ``promo:{code_id}:{tenant_id}``,
so a retry never double-writes; only a platform admin redeems one. A channel code
(``channel_cap_usd`` set) also requires a platform admin: each redemption writes a ledger
credit attributed to the member's channel, keyed by code, tenant, channel and
person, and raises that channel's budget limit by the same amount when it has
one. The balance stays ``SUM(delta_usd)`` and the balance and cap gates never
look at promo state. Timed windows are settled by
``daimon.core.promo_settlement``.

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
    channel_cap_refusal,
    hash_promo_code,
    is_channel_code,
    is_granted_on_redeem,
    is_well_formed_promo_code,
    normalize_promo_code,
    redeem_refusal,
)
from daimon.core.stores import channel_budgets as budget_store
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores import tenant_ledger
from daimon.core.stores.domain import PromoCodeKind, PromoCodeRow, TimedPromoGrantRow
from daimon.core.usage_recording import SPEND_LEDGER_REASONS
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

# Refused attempts one person may make per window before their redemption pauses.
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
    channel_id: str | None = None
    """The channel a channel code credited; None for a server-wide redemption."""
    channel_limit_usd: Decimal | None = None
    """That channel's budget limit after the raise; None when it has no budget."""


@dataclasses.dataclass(frozen=True)
class PromoChannel:
    """Where a member redeems: the channel the panel was opened in (a thread's parent)."""

    platform: str
    channel_id: str


@dataclasses.dataclass(frozen=True)
class PromoRedeemRefused:
    reason: PromoRefusal


PromoRedeemResult = PromoRedeemed | PromoRedeemRefused


@dataclasses.dataclass(frozen=True)
class ActiveTimedCredit:
    remaining_usd: Decimal
    ends_at: datetime


async def grant_promo_credit(
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
    channel: PromoChannel | None,
    is_admin: bool,
) -> PromoRedeemResult:
    """Redeem ``code`` for the tenant in one transaction; a refusal is counted for throttling.

    ``is_admin``: whether the caller found the redeemer to be a server or
    workspace admin, live, at submit; only a platform admin may redeem any
    code. ``channel``: where the redeemer is, a thread's parent channel; a
    channel code lands there for a platform admin and is refused without
    one. Both are required so no caller can grant admin rights by omission.

    The redeemer's attempts are serialized first, so concurrent guesses cannot
    all pass the throttle on the same count. Refusals are counted per person
    (``account_id``) within the tenant: one member's refusals never pause
    another member's, or an admin's, redemptions. The code row is then locked, so
    the per-code limit and the one-per-tenant rule are applied serially too.
    """
    since = now - REDEEM_FAILURE_WINDOW
    async with session_factory() as session, session.begin():
        await promo_store.lock_redeemer(session, tenant_id=tenant_id, account_id=account_id)
        failures = await promo_store.count_redeem_failures(
            session, tenant_id=tenant_id, account_id=account_id, since=since
        )
        if failures >= REDEEM_FAILURE_LIMIT:
            log.warning("promo_code.redeem_throttled", tenant_id=str(tenant_id))
            return PromoRedeemRefused(reason="throttled")
        result = await _redeem_in_session(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            code=code,
            now=now,
            channel=channel,
            is_admin=is_admin,
        )
        if isinstance(result, PromoRedeemRefused):
            await promo_store.record_redeem_failure(
                session,
                tenant_id=tenant_id,
                account_id=account_id,
                now=now,
                prune_before=since,
            )
            log.info("promo_code.redeem_refused", tenant_id=str(tenant_id), reason=result.reason)
        else:
            log.info(
                "promo_code.redeemed",
                tenant_id=str(tenant_id),
                promo_code_id=str(result.promo_code_id),
                kind=result.kind,
                granted=result.granted,
                channel_id=result.channel_id,
            )
        return result


async def _redeem_in_session(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    code: str,
    now: datetime,
    channel: PromoChannel | None,
    is_admin: bool,
) -> PromoRedeemResult:
    if is_admin is not True:
        return PromoRedeemRefused(reason="admin_only")
    normalized = normalize_promo_code(code)
    if not is_well_formed_promo_code(normalized):
        return PromoRedeemRefused(reason="invalid")
    row = await promo_store.lock_promo_code_by_hash(session, hash_promo_code(normalized))
    if row is None:
        return PromoRedeemRefused(reason="invalid")
    if is_channel_code(row):
        return await _redeem_into_channel(
            session, row=row, tenant_id=tenant_id, account_id=account_id, now=now, channel=channel
        )
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
        await grant_promo_credit(
            session, promo_code_id=row.id, tenant_id=tenant_id, amount_usd=row.amount_usd
        )
    return PromoRedeemed(
        promo_code_id=row.id,
        kind=row.kind,
        amount_usd=row.amount_usd,
        credit_starts_at=row.credit_starts_at,
        credit_ends_at=row.credit_ends_at,
        granted=granted,
        balance_usd=await tenant_ledger.get_balance(session, tenant_id=tenant_id),
    )


async def _redeem_into_channel(
    session: AsyncSession,
    *,
    row: PromoCodeRow,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    now: datetime,
    channel: PromoChannel | None,
) -> PromoRedeemResult:
    """Credit a channel code to ``channel``: once per person there, up to the code's channel cap."""
    if channel is None:
        return PromoRedeemRefused(reason="needs_channel")
    if account_id is None:
        raise ValueError("a channel redemption needs the redeeming account")
    channel_id = channel.channel_id
    if await promo_store.has_channel_redemption(
        session,
        promo_code_id=row.id,
        tenant_id=tenant_id,
        channel_id=channel_id,
        account_id=account_id,
    ):
        return PromoRedeemRefused(reason="already_redeemed_in_channel")
    refusal = redeem_refusal(row, now=now)
    if refusal is not None:
        return PromoRedeemRefused(reason=refusal)
    taken = await promo_store.count_channel_redemptions(
        session, promo_code_id=row.id, tenant_id=tenant_id, channel_id=channel_id
    )
    refusal = channel_cap_refusal(row, channel_redeemed_usd=row.amount_usd * taken)
    if refusal is not None:
        return PromoRedeemRefused(reason=refusal)
    if not await promo_store.insert_channel_redemption(
        session,
        promo_code_id=row.id,
        tenant_id=tenant_id,
        channel_id=channel_id,
        account_id=account_id,
        now=now,
    ):
        return PromoRedeemRefused(reason="already_redeemed_in_channel")
    await tenant_ledger.insert_entry(
        session,
        tenant_id=tenant_id,
        delta_usd=row.amount_usd,
        reason="promo_credit",
        idempotency_key=f"promo:{row.id}:{tenant_id}:{channel_id}:{account_id}",
        channel_id=channel_id,
    )
    budget = await budget_store.raise_channel_budget(
        session,
        tenant_id=tenant_id,
        platform=channel.platform,
        channel_id=channel_id,
        amount_usd=row.amount_usd,
    )
    return PromoRedeemed(
        promo_code_id=row.id,
        kind=row.kind,
        amount_usd=row.amount_usd,
        credit_starts_at=None,
        credit_ends_at=None,
        granted=True,
        balance_usd=await tenant_ledger.get_balance(session, tenant_id=tenant_id),
        channel_id=channel_id,
        channel_limit_usd=None if budget is None else budget.limit_usd,
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


async def unspent_timed_credit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    targets: Collection[uuid.UUID],
    horizon: datetime,
) -> dict[uuid.UUID, Decimal]:
    """Unspent timed credit per code at ``horizon``, from the spend recorded so far."""
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
    remaining = await unspent_timed_credit(session, tenant_id=tenant_id, targets=live, horizon=now)
    credits = [
        ActiveTimedCredit(
            remaining_usd=remaining.get(code_id, row.amount_usd), ends_at=row.credit_ends_at
        )
        for code_id, row in live.items()
    ]
    return sorted(credits, key=lambda credit: credit.ends_at)
