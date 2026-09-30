"""Redeeming promo codes and settling timed credit against the tenant ledger."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from daimon.core._models import TenantLedger
from daimon.core.promo_codes import build_promo_code_terms, hash_promo_code, normalize_promo_code
from daimon.core.promo_credit import (
    REDEEM_FAILURE_LIMIT,
    REDEEM_FAILURE_WINDOW,
    ActiveTimedCredit,
    PromoRedeemed,
    PromoRedeemRefused,
    PromoRedeemResult,
    PromoSettlement,
    get_active_timed_credit,
    redeem_promo_code,
    settle_promo_credit,
)
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores import tenant_ledger
from daimon.core.stores.domain import PromoCodeRow, TenantRow
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

T0 = datetime(2026, 5, 1, tzinfo=UTC)
H = timedelta(hours=1)
Factory = async_sessionmaker[AsyncSession]


async def _create(session: AsyncSession, code: str = "SPRING-2026", **kwargs: Any) -> PromoCodeRow:
    kwargs.setdefault("amount_usd", Decimal("10"))
    kwargs.setdefault("timed", False)
    row = await promo_store.insert_promo_code(
        session,
        code_hash=hash_promo_code(normalize_promo_code(code)),
        terms=build_promo_code_terms(**kwargs),
    )
    assert row is not None
    return row


async def _timed(session: AsyncSession, code: str, start: int, end: int) -> PromoCodeRow:
    return await _create(
        session, code, timed=True, credit_starts_at=T0 + start * H, credit_ends_at=T0 + end * H
    )


async def _spend(session: AsyncSession, tenant: TenantRow, amount: str, at: datetime) -> None:
    session.add(
        TenantLedger(
            tenant_id=tenant.id,
            delta_usd=-Decimal(amount),
            reason="turn_debit",
            idempotency_key=f"turn:{uuid.uuid4()}",
            occurred_at=at,
        )
    )
    await session.flush()


async def _redeem(
    factory: Factory, tenant: TenantRow, code: str, now: datetime = T0
) -> PromoRedeemResult:
    return await redeem_promo_code(
        factory, tenant_id=tenant.id, account_id=None, code=code, now=now
    )


def _settled(granted: int, expired: int, usd: str = "0") -> PromoSettlement:
    return PromoSettlement(granted, expired, Decimal(usd))


async def _ledger(session: AsyncSession, tenant: TenantRow) -> dict[str, Decimal]:
    rows = await tenant_ledger.list_for_tenant(session, tenant_id=tenant.id)
    return {r.idempotency_key: r.delta_usd for r in rows}


async def test_nothing_changes_without_promo_codes(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant = await make_tenant(db_session)
    assert await settle_promo_credit(db_session_factory, now=T0) == _settled(0, 0)
    assert await get_active_timed_credit(db_session, tenant_id=tenant.id, now=T0) == []
    assert await _redeem(db_session_factory, tenant, "NOSUCHCODE") == PromoRedeemRefused("invalid")
    assert await _ledger(db_session, tenant) == {}


async def test_credit_code_grants_once_per_tenant(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant = await make_tenant(db_session, workspace_id="g1")
    other = await make_tenant(db_session, workspace_id="g2")
    account = await make_account(db_session, tenant=tenant)
    code = await _create(db_session, max_redemptions=5)

    result = await redeem_promo_code(
        db_session_factory, tenant_id=tenant.id, account_id=account.id, code=" spring-2026 ", now=T0
    )
    assert isinstance(result, PromoRedeemed)
    assert (result.granted, result.balance_usd) == (True, Decimal("10"))
    assert await _ledger(db_session, tenant) == {f"promo:{code.id}:{tenant.id}": Decimal("10")}
    assert await _redeem(db_session_factory, tenant, "SPRING2026") == PromoRedeemRefused(
        "already_redeemed"
    )
    assert isinstance(await _redeem(db_session_factory, other, "SPRING2026", T0 + H), PromoRedeemed)

    redemptions = await promo_store.list_redemptions(db_session, promo_code_id=code.id)
    assert [(r.tenant_external_id, r.redeemed_by_account_id) for r in redemptions] == [
        ("g1", account.id),
        ("g2", None),
    ]
    stored = await promo_store.get_promo_code(db_session, code.id)
    assert stored is not None and stored.redeemed_count == 2


async def test_refusals(db_session: AsyncSession, db_session_factory: Factory) -> None:
    tenants = [await make_tenant(db_session, workspace_id=f"g{i}") for i in range(3)]
    await _create(db_session, "ONLYONE", max_redemptions=1)
    revoked = await _create(db_session, "REVOKED")
    await promo_store.revoke_promo_code(db_session, promo_code_id=revoked.id, now=T0)
    await _create(db_session, "NOTYET", redeem_starts_at=T0 + H)
    await _create(db_session, "OVERDUE", redeem_ends_at=T0)

    assert isinstance(await _redeem(db_session_factory, tenants[0], "ONLYONE"), PromoRedeemed)
    refusals = [
        await _redeem(db_session_factory, tenants[1], code)
        for code in ("ONLYONE", "REVOKED", "NOTYET", "OVERDUE", "x!")
    ]
    assert refusals == [
        PromoRedeemRefused(reason)
        for reason in ("exhausted", "revoked", "not_started", "expired", "invalid")
    ]
    assert await _ledger(db_session, tenants[1]) == {}


async def test_repeated_failures_pause_redemption_for_the_window(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant = await make_tenant(db_session, workspace_id="g1")
    other = await make_tenant(db_session, workspace_id="g2")
    await _create(db_session)
    for _ in range(REDEEM_FAILURE_LIMIT):
        assert await _redeem(db_session_factory, tenant, "WRONGCODE") == PromoRedeemRefused(
            "invalid"
        )
    assert await _redeem(db_session_factory, tenant, "SPRING2026") == PromoRedeemRefused(
        "throttled"
    )
    assert isinstance(await _redeem(db_session_factory, other, "SPRING2026"), PromoRedeemed)
    later = T0 + REDEEM_FAILURE_WINDOW + timedelta(seconds=1)
    assert isinstance(await _redeem(db_session_factory, tenant, "SPRING2026", later), PromoRedeemed)


async def test_timed_code_is_granted_when_its_window_opens(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant = await make_tenant(db_session)
    code = await _timed(db_session, "TIMED1", 2, 6)
    result = await _redeem(db_session_factory, tenant, "TIMED1")
    assert isinstance(result, PromoRedeemed) and not result.granted
    assert await _ledger(db_session, tenant) == {}

    assert await settle_promo_credit(db_session_factory, now=T0 + H) == _settled(0, 0)
    assert await settle_promo_credit(db_session_factory, now=T0 + 2 * H) == _settled(1, 0)
    assert await settle_promo_credit(db_session_factory, now=T0 + 3 * H) == _settled(0, 0)
    assert await _ledger(db_session, tenant) == {f"promo:{code.id}:{tenant.id}": Decimal("10")}


async def test_timed_code_redeemed_inside_its_window_grants_at_once(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant = await make_tenant(db_session)
    await _timed(db_session, "TIMED1", 0, 6)
    result = await _redeem(db_session_factory, tenant, "TIMED1", T0 + H)
    assert isinstance(result, PromoRedeemed) and result.granted
    assert result.balance_usd == Decimal("10")
    assert await _redeem(db_session_factory, tenant, "TIMED1", T0 + 6 * H) == PromoRedeemRefused(
        "already_redeemed"
    )


async def test_expiry_removes_only_the_unspent_remainder(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant = await make_tenant(db_session)
    code = await _timed(db_session, "TIMED1", 0, 6)
    await _redeem(db_session_factory, tenant, "TIMED1")
    await _spend(db_session, tenant, "1", T0 - H)  # before the window: not drawn from the grant
    await _spend(db_session, tenant, "3", T0 + 2 * H)
    await _spend(db_session, tenant, "5", T0 + 7 * H)  # after it closed

    active = await get_active_timed_credit(db_session, tenant_id=tenant.id, now=T0 + 3 * H)
    assert active == [ActiveTimedCredit(remaining_usd=Decimal("7"), ends_at=T0 + 6 * H)]

    settled = await settle_promo_credit(db_session_factory, now=T0 + 8 * H)
    assert settled == _settled(0, 1, "7")
    assert (await _ledger(db_session, tenant))[f"promo_expiry:{code.id}:{tenant.id}"] == Decimal(
        "-7"
    )
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == Decimal("-6")
    assert await settle_promo_credit(db_session_factory, now=T0 + 9 * H) == _settled(0, 0)
    assert await get_active_timed_credit(db_session, tenant_id=tenant.id, now=T0 + 3 * H) == []
    [redemption] = await promo_store.list_redemptions(db_session, promo_code_id=code.id)
    assert redemption.expired_usd == Decimal("7")


async def test_overlapping_timed_credit_is_spent_earliest_ending_first(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant = await make_tenant(db_session)
    long = await _timed(db_session, "LONGER", 0, 10)
    short = await _timed(db_session, "SHORT1", 2, 6)
    await _redeem(db_session_factory, tenant, "LONGER")
    await _redeem(db_session_factory, tenant, "SHORT1")
    await settle_promo_credit(db_session_factory, now=T0 + 2 * H)
    await _spend(db_session, tenant, "12", T0 + 3 * H)

    active = await get_active_timed_credit(db_session, tenant_id=tenant.id, now=T0 + 4 * H)
    assert [c.remaining_usd for c in active] == [Decimal("0"), Decimal("8")]

    assert await settle_promo_credit(db_session_factory, now=T0 + 6 * H) == _settled(0, 1)
    assert await settle_promo_credit(db_session_factory, now=T0 + 10 * H) == _settled(0, 1, "8")
    ledger = await _ledger(db_session, tenant)
    assert f"promo_expiry:{short.id}:{tenant.id}" not in ledger
    assert ledger[f"promo_expiry:{long.id}:{tenant.id}"] == Decimal("-8")


async def test_window_missed_entirely_is_closed_without_credit(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant = await make_tenant(db_session)
    await _timed(db_session, "TIMED1", 2, 6)
    await _redeem(db_session_factory, tenant, "TIMED1")
    assert await settle_promo_credit(db_session_factory, now=T0 + 7 * H) == _settled(0, 0)
    assert await _ledger(db_session, tenant) == {}
    assert await settle_promo_credit(db_session_factory, now=T0 + 8 * H) == _settled(0, 0)
