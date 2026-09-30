"""Redeeming promo codes and settling timed credit against the tenant ledger."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from daimon.core._models import TenantLedger
from daimon.core.promo_codes import (
    PromoCodeTerms,
    build_promo_code_terms,
    hash_promo_code,
    normalize_promo_code,
)
from daimon.core.promo_credit import (
    PROMO_EXPIRY_GRACE,
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
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
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
    assert row is not None, "the promo code insert should return a row"
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
    """With no code created, settlement, panels, joins and the ledger see nothing."""
    tenant = await make_tenant(db_session)
    assert await settle_promo_credit(db_session_factory, now=T0) == _settled(0, 0), (
        "settlement should be a no-op"
    )
    assert await get_active_timed_credit(db_session, tenant_id=tenant.id, now=T0) == [], (
        "panels should show no timed credit"
    )
    assert not await promo_store.has_redeemable_promo_code(db_session, now=T0), (
        "join messages should not mention promo codes"
    )
    assert await _redeem(db_session_factory, tenant, "NOSUCHCODE") == PromoRedeemRefused(
        "invalid"
    ), "an attempt should be refused as invalid"
    assert await _ledger(db_session, tenant) == {}, "the ledger should stay empty"


async def test_credit_code_grants_once_per_tenant(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    """A credit code grants once per tenant and records each redemption."""
    tenant = await make_tenant(db_session, workspace_id="g1")
    other = await make_tenant(db_session, workspace_id="g2")
    account = await make_account(db_session, tenant=tenant)
    code = await _create(db_session, max_redemptions=5)

    result = await redeem_promo_code(
        db_session_factory, tenant_id=tenant.id, account_id=account.id, code=" spring-2026 ", now=T0
    )
    assert isinstance(result, PromoRedeemed), "the first redemption should succeed"
    assert (result.granted, result.balance_usd) == (True, Decimal("10")), (
        "credit should be granted at once"
    )
    assert await _ledger(db_session, tenant) == {f"promo:{code.id}:{tenant.id}": Decimal("10")}, (
        "the ledger should hold one promo grant"
    )
    assert await _redeem(db_session_factory, tenant, "SPRING2026") == PromoRedeemRefused(
        "already_redeemed"
    ), "the same tenant should not redeem twice"
    assert isinstance(
        await _redeem(db_session_factory, other, "SPRING2026", T0 + H), PromoRedeemed
    ), "another tenant should still redeem"

    redemptions = await promo_store.list_redemptions(db_session, promo_code_id=code.id)
    assert [(r.tenant_external_id, r.redeemed_by_account_id) for r in redemptions] == [
        ("g1", account.id),
        ("g2", None),
    ], "both redemptions should be recorded with who redeemed"
    stored = await promo_store.get_promo_code(db_session, code.id)
    assert stored is not None and stored.redeemed_count == 2, "the count should be 2"


async def test_refusals(db_session: AsyncSession, db_session_factory: Factory) -> None:
    """Exhausted, revoked, not-yet-open, expired and malformed codes are refused."""
    tenants = [await make_tenant(db_session, workspace_id=f"g{i}") for i in range(3)]
    await _create(db_session, "ONLYONE", max_redemptions=1)
    revoked = await _create(db_session, "REVOKED")
    await promo_store.revoke_promo_code(db_session, promo_code_id=revoked.id, now=T0)
    await _create(db_session, "NOTYET", redeem_starts_at=T0 + H)
    await _create(db_session, "OVERDUE", redeem_ends_at=T0)

    assert isinstance(await _redeem(db_session_factory, tenants[0], "ONLYONE"), PromoRedeemed), (
        "the single use should succeed"
    )
    refusals = [
        await _redeem(db_session_factory, tenants[1], code)
        for code in ("ONLYONE", "REVOKED", "NOTYET", "OVERDUE", "x!")
    ]
    assert refusals == [
        PromoRedeemRefused(reason)
        for reason in ("exhausted", "revoked", "not_started", "expired", "invalid")
    ], "each code should be refused for its own reason"
    assert await _ledger(db_session, tenants[1]) == {}, "refusals should not touch the ledger"


async def test_repeated_failures_pause_redemption_for_the_window(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    """Repeated failures throttle only that tenant, until the window passes."""
    tenant = await make_tenant(db_session, workspace_id="g1")
    other = await make_tenant(db_session, workspace_id="g2")
    await _create(db_session)
    for _ in range(REDEEM_FAILURE_LIMIT):
        assert await _redeem(db_session_factory, tenant, "WRONGCODE") == PromoRedeemRefused(
            "invalid"
        ), "a wrong code should be refused as invalid"
    assert await _redeem(db_session_factory, tenant, "SPRING2026") == PromoRedeemRefused(
        "throttled"
    ), "even a valid code should be throttled after the limit"
    assert isinstance(await _redeem(db_session_factory, other, "SPRING2026"), PromoRedeemed), (
        "other tenants should not be throttled"
    )
    later = T0 + REDEEM_FAILURE_WINDOW + timedelta(seconds=1)
    assert isinstance(
        await _redeem(db_session_factory, tenant, "SPRING2026", later), PromoRedeemed
    ), "redemption should resume after the window"


async def test_timed_code_is_granted_when_its_window_opens(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    """A timed code redeemed early is granted once, by settlement, when its window opens."""
    tenant = await make_tenant(db_session)
    code = await _timed(db_session, "TIMED1", 2, 6)
    result = await _redeem(db_session_factory, tenant, "TIMED1")
    assert isinstance(result, PromoRedeemed) and not result.granted, (
        "early redemption should defer the grant"
    )
    assert await _ledger(db_session, tenant) == {}, "nothing should be credited yet"

    assert await settle_promo_credit(db_session_factory, now=T0 + H) == _settled(0, 0), (
        "nothing should be granted before the window"
    )
    assert await settle_promo_credit(db_session_factory, now=T0 + 2 * H) == _settled(1, 0), (
        "the grant should settle when the window opens"
    )
    assert await settle_promo_credit(db_session_factory, now=T0 + 3 * H) == _settled(0, 0), (
        "the grant should settle only once"
    )
    assert await _ledger(db_session, tenant) == {f"promo:{code.id}:{tenant.id}": Decimal("10")}, (
        "the ledger should hold one grant"
    )


async def test_timed_code_redeemed_inside_its_window_grants_at_once(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    """A timed code redeemed inside its window is credited immediately."""
    tenant = await make_tenant(db_session)
    await _timed(db_session, "TIMED1", 0, 6)
    result = await _redeem(db_session_factory, tenant, "TIMED1", T0 + H)
    assert isinstance(result, PromoRedeemed) and result.granted, "the grant should be immediate"
    assert result.balance_usd == Decimal("10"), "the balance should include the credit"
    assert await _redeem(db_session_factory, tenant, "TIMED1", T0 + 6 * H) == PromoRedeemRefused(
        "already_redeemed"
    ), "a second redemption should be refused"


async def test_expiry_removes_only_the_unspent_remainder(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    """Expiry removes only the credit left after spend inside the window."""
    tenant = await make_tenant(db_session)
    code = await _timed(db_session, "TIMED1", 0, 6)
    await _redeem(db_session_factory, tenant, "TIMED1")
    await _spend(db_session, tenant, "1", T0 - H)  # before the window: not drawn from the grant
    await _spend(db_session, tenant, "3", T0 + 2 * H)
    await _spend(db_session, tenant, "5", T0 + 7 * H)  # after it closed

    active = await get_active_timed_credit(db_session, tenant_id=tenant.id, now=T0 + 3 * H)
    assert active == [ActiveTimedCredit(remaining_usd=Decimal("7"), ends_at=T0 + 6 * H)], (
        "only spend inside the window should draw on the credit"
    )

    settled = await settle_promo_credit(db_session_factory, now=T0 + 8 * H)
    assert settled == _settled(0, 1, "7"), "the unspent $7 should expire"
    assert (await _ledger(db_session, tenant))[f"promo_expiry:{code.id}:{tenant.id}"] == Decimal(
        "-7"
    ), "the expiry entry should remove $7"
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == Decimal("-6"), (
        "spend outside the window should stay on the balance"
    )
    assert await settle_promo_credit(db_session_factory, now=T0 + 9 * H) == _settled(0, 0), (
        "expiry should settle only once"
    )
    assert await get_active_timed_credit(db_session, tenant_id=tenant.id, now=T0 + 3 * H) == [], (
        "expired credit should no longer show as active"
    )
    [redemption] = await promo_store.list_redemptions(db_session, promo_code_id=code.id)
    assert redemption.expired_usd == Decimal("7"), "the redemption should record the expired $7"


async def test_overlapping_timed_credit_is_spent_earliest_ending_first(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    """Overlapping timed credit is spent earliest-ending first."""
    tenant = await make_tenant(db_session)
    long = await _timed(db_session, "LONGER", 0, 10)
    short = await _timed(db_session, "SHORT1", 2, 6)
    await _redeem(db_session_factory, tenant, "LONGER")
    await _redeem(db_session_factory, tenant, "SHORT1")
    await settle_promo_credit(db_session_factory, now=T0 + 2 * H)
    await _spend(db_session, tenant, "12", T0 + 3 * H)

    active = await get_active_timed_credit(db_session, tenant_id=tenant.id, now=T0 + 4 * H)
    assert [c.remaining_usd for c in active] == [Decimal("0"), Decimal("8")], (
        "the shorter grant should be spent first"
    )

    after_short = T0 + 6 * H + PROMO_EXPIRY_GRACE
    after_long = T0 + 10 * H + PROMO_EXPIRY_GRACE
    assert await settle_promo_credit(db_session_factory, now=after_short) == _settled(0, 1), (
        "the empty short grant should expire with nothing left"
    )
    assert await settle_promo_credit(db_session_factory, now=after_long) == _settled(0, 1, "8"), (
        "the long grant's $8 remainder should expire"
    )
    ledger = await _ledger(db_session, tenant)
    assert f"promo_expiry:{short.id}:{tenant.id}" not in ledger, (
        "an empty grant should write no expiry entry"
    )
    assert ledger[f"promo_expiry:{long.id}:{tenant.id}"] == Decimal("-8"), (
        "the long grant's expiry should remove $8"
    )


async def test_window_missed_entirely_is_closed_without_credit(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    """A timed code whose window passes before settlement closes without credit."""
    tenant = await make_tenant(db_session)
    await _timed(db_session, "TIMED1", 2, 6)
    await _redeem(db_session_factory, tenant, "TIMED1")
    assert await settle_promo_credit(db_session_factory, now=T0 + 7 * H) == _settled(0, 0), (
        "a missed window should neither grant nor expire"
    )
    assert await _ledger(db_session, tenant) == {}, "a missed window should credit nothing"
    assert await settle_promo_credit(db_session_factory, now=T0 + 8 * H) == _settled(0, 0), (
        "the closed redemption should not settle again"
    )


def _terms(amount: str, *, start: int, end: int) -> PromoCodeTerms:
    """Timed terms built directly, skipping validation, to reach the database's own checks."""
    return PromoCodeTerms(
        amount_usd=Decimal(amount),
        kind="timed",
        note=None,
        credit_starts_at=T0 + start * H,
        credit_ends_at=T0 + end * H,
        redeem_starts_at=None,
        redeem_ends_at=None,
        max_redemptions=None,
    )


async def test_the_database_refuses_an_amount_the_ledger_cannot_hold(
    db_session: AsyncSession,
) -> None:
    """The amount CHECK backs up validation for rows written around it."""
    with pytest.raises(IntegrityError, match="ck_promo_codes_amount_range"):
        await promo_store.insert_promo_code(
            db_session, code_hash="h", terms=_terms("1000000", start=0, end=1)
        )


@pytest.mark.fresh_schema  # drops a CHECK to plant a row the ledger rejects
async def test_a_row_the_ledger_rejects_does_not_hold_back_settlement(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    """The bad grant is logged and stays due; other grants and expiries still settle."""
    tenant = await make_tenant(db_session)
    await db_session.execute(
        text("ALTER TABLE promo_codes DROP CONSTRAINT ck_promo_codes_amount_range")
    )
    huge = await promo_store.insert_promo_code(
        db_session,
        code_hash=hash_promo_code(normalize_promo_code("HUGEAMOUNT")),
        terms=_terms("9999999999", start=0, end=5),
    )
    assert huge is not None, "the unchecked insert should succeed"
    await _timed(db_session, "GOODGRANT", 0, 5)
    await _timed(db_session, "EARLYWINDOW", -3, -2)
    for code, at in (("HUGEAMOUNT", T0 - H), ("GOODGRANT", T0 - H), ("EARLYWINDOW", T0 - 3 * H)):
        result = await _redeem(db_session_factory, tenant, code, now=at)
        assert isinstance(result, PromoRedeemed), f"{code} should redeem"

    settled = await settle_promo_credit(db_session_factory, now=T0 + H)

    assert settled == _settled(1, 1, "10"), "the good grant and the expiry should both settle"
    granted = await promo_store.list_timed_grants(db_session, tenant_id=tenant.id)
    assert huge.id not in {g.promo_code_id for g in granted}, "the rejected grant stays due"
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == Decimal("10"), (
        "only the good grant should remain on the ledger"
    )


class _FirstSessionFails(async_sessionmaker[AsyncSession]):
    """Fails the first session it opens, as a dropped connection would."""

    opened = 0

    def __call__(self, **local_kw: Any) -> AsyncSession:
        self.opened += 1
        if self.opened == 1:
            raise OperationalError("SELECT 1", {}, ConnectionError("connection lost"))
        return super().__call__(**local_kw)


async def test_expiries_settle_even_when_the_grant_phase_fails(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    """The grant phase's error is raised only after the expiry phase has run."""
    tenant = await make_tenant(db_session)
    await _timed(db_session, "EARLYWINDOW", -3, -2)
    await _redeem(db_session_factory, tenant, "EARLYWINDOW", now=T0 - 3 * H)
    factory = _FirstSessionFails(bind=db_session.bind, expire_on_commit=False)

    with pytest.raises(OperationalError):
        await settle_promo_credit(factory, now=T0 + H)

    [grant] = await promo_store.list_timed_grants(db_session, tenant_id=tenant.id)
    assert grant.expired_at is not None, "the closed window should still expire"
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == 0, (
        "the unspent timed credit should be gone"
    )


async def test_settlement_works_through_every_due_row_in_batches(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    """A backlog larger than one batch is settled in a single call."""
    tenants = [await make_tenant(db_session) for _ in range(3)]
    await _timed(db_session, "BACKLOG", 1, 5)
    for tenant in tenants:
        await _redeem(db_session_factory, tenant, "BACKLOG")

    settled = await settle_promo_credit(db_session_factory, now=T0 + 2 * H, limit=2)

    assert settled == _settled(3, 0), "all three due grants should settle despite limit=2"
    settled = await settle_promo_credit(db_session_factory, now=T0 + 6 * H, limit=2)
    assert settled == _settled(0, 3, "30"), "all three expiries should settle despite limit=2"


async def test_expiry_waits_out_the_grace_period_for_late_recorded_spend(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    """Spend dated inside the window but written after it closed still draws on the credit."""
    tenant = await make_tenant(db_session)
    await _timed(db_session, "LATESPEND", 1, 5)
    await _redeem(db_session_factory, tenant, "LATESPEND")
    await settle_promo_credit(db_session_factory, now=T0 + 2 * H)
    closed = T0 + 5 * H

    early = await settle_promo_credit(db_session_factory, now=closed + PROMO_EXPIRY_GRACE / 2)
    await _spend(db_session, tenant, "4", at=T0 + 4 * H)  # recorded late, dated in the window
    settled = await settle_promo_credit(db_session_factory, now=closed + PROMO_EXPIRY_GRACE)

    assert early == _settled(0, 0), "nothing should expire inside the grace period"
    assert settled == _settled(0, 1, "6"), "only the unspent $6 should expire"
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == 0, (
        "the late spend should have been paid from the timed credit"
    )
