"""Promo redemption under real, separate transactions: throttle scope, caps and limits.

The standard ``db_session_factory`` binds every session to one connection, so
it cannot show a race or a lock. These tests seed committed rows and redeem
through an engine-backed factory, one pooled transaction per attempt.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core.promo_codes import build_promo_code_terms, hash_promo_code, normalize_promo_code
from daimon.core.promo_credit import (
    REDEEM_FAILURE_LIMIT,
    PromoChannel,
    PromoRedeemed,
    PromoRedeemRefused,
    PromoRedeemResult,
    redeem_promo_code,
)
from daimon.core.stores import channel_budgets, promo_codes, tenant_ledger
from daimon.core.stores.domain import AccountRow, PromoCodeRow, TenantRow
from daimon.testing.factories import make_account, make_channel_budget, make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.usefixtures("db_clean")

NOW = datetime(2026, 10, 12, tzinfo=UTC)
Factory = async_sessionmaker[AsyncSession]


async def _seed(
    engine: AsyncEngine, *, cap: str = "20", max_redemptions: int | None = None, tenants: int = 1
) -> tuple[Factory, list[TenantRow], list[AccountRow], PromoCodeRow]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        rooms = [await make_tenant(session, workspace_id=str(100 + i)) for i in range(tenants)]
        people = [await make_account(session, tenant=rooms[0]) for _ in range(4)]
        for room in rooms:
            await make_channel_budget(session, tenant=room, channel_id="c1")
        code = await promo_codes.insert_promo_code(
            session,
            code_hash=hash_promo_code(normalize_promo_code("CHANNEL-CODE-2026")),
            terms=build_promo_code_terms(
                amount_usd=Decimal("10"),
                timed=False,
                channel_cap_usd=Decimal(cap),
                max_redemptions=max_redemptions,
            ),
        )
        await promo_codes.insert_promo_code(
            session,
            code_hash=hash_promo_code(normalize_promo_code("SERVER-CODE-2026")),
            terms=build_promo_code_terms(amount_usd=Decimal("10"), timed=False),
        )
    assert code is not None
    return factory, rooms, people, code


async def _redeem(
    factory: Factory,
    room: TenantRow,
    person: AccountRow,
    *,
    channel: str = "c1",
    code: str = "CHANNEL-CODE-2026",
    admin: bool = True,
) -> PromoRedeemResult:
    return await redeem_promo_code(
        factory,
        tenant_id=room.id,
        account_id=person.id,
        code=code,
        now=NOW,
        channel=PromoChannel(platform="discord", channel_id=channel),
        is_admin=admin,
    )


async def test_one_members_refusals_never_throttle_another_member_or_an_admin(
    db_engine: AsyncEngine,
) -> None:
    """After one member hits the refusal limit, others in the server redeem as normal."""
    factory, rooms, people, _ = await _seed(db_engine)
    room, guesser, member, admin = rooms[0], people[0], people[1], people[2]
    for _ in range(REDEEM_FAILURE_LIMIT):
        assert await _redeem(factory, room, guesser, code="NOT-A-REAL-CODE") == (
            PromoRedeemRefused("invalid")
        )

    paused = await _redeem(factory, room, guesser)
    other_member = await _redeem(factory, room, member, channel="c2")
    server_wide = await _redeem(factory, room, admin, code="SERVER-CODE-2026", admin=True)

    assert paused == PromoRedeemRefused("throttled"), "the guesser is paused, even for a real code"
    assert isinstance(other_member, PromoRedeemed), "another member is not"
    assert isinstance(server_wide, PromoRedeemed), "an admin's server-wide top-up is not"


async def test_refusals_spread_across_members_pause_none_of_them(db_engine: AsyncEngine) -> None:
    """Five ordinary refusals by different people do not use up a shared quota."""
    factory, rooms, people, _ = await _seed(db_engine)
    room = rooms[0]
    for person in people:
        await _redeem(factory, room, person, code="NOT-A-REAL-CODE")
    await _redeem(factory, room, people[0], code="NOT-A-REAL-CODE")

    result = await _redeem(factory, room, people[1])

    assert isinstance(result, PromoRedeemed), result


async def test_concurrent_members_never_pass_the_channel_cap(db_engine: AsyncEngine) -> None:
    """Four people at once on a $20 cap with $10 each: exactly two are credited."""
    factory, rooms, people, code = await _seed(db_engine)
    results = await asyncio.gather(*(_redeem(factory, rooms[0], person) for person in people))

    assert sum(isinstance(r, PromoRedeemed) for r in results) == 2
    assert results.count(PromoRedeemRefused("channel_cap_reached")) == 2
    async with factory() as session:
        assert await tenant_ledger.get_balance(session, tenant_id=rooms[0].id) == Decimal("20")
        budget = await channel_budgets.get_channel_budget(
            session, tenant_id=rooms[0].id, platform="discord", channel_id="c1"
        )
        assert budget is not None and budget.limit_usd == Decimal("25")
        assert len(await promo_codes.list_redemptions(session, promo_code_id=code.id)) == 2


async def test_concurrent_repeats_by_one_person_credit_once(db_engine: AsyncEngine) -> None:
    factory, rooms, people, code = await _seed(db_engine)
    results = await asyncio.gather(*(_redeem(factory, rooms[0], people[0]) for _ in range(3)))

    assert sum(isinstance(r, PromoRedeemed) for r in results) == 1
    assert results.count(PromoRedeemRefused("already_redeemed_in_channel")) == 2
    async with factory() as session:
        assert await tenant_ledger.get_balance(session, tenant_id=rooms[0].id) == Decimal("10")
        assert len(await promo_codes.list_redemptions(session, promo_code_id=code.id)) == 1


async def test_concurrent_tenants_share_the_overall_limit(db_engine: AsyncEngine) -> None:
    """--max-redemptions 1 admits one redemption across every tenant at once."""
    factory, rooms, people, code = await _seed(db_engine, max_redemptions=1, tenants=3)
    results = await asyncio.gather(*(_redeem(factory, room, people[0]) for room in rooms))

    assert sum(isinstance(r, PromoRedeemed) for r in results) == 1
    assert results.count(PromoRedeemRefused("exhausted")) == 2
    async with factory() as session:
        stored = await promo_codes.get_promo_code(session, code.id)
        assert stored is not None and stored.redeemed_count == 1


async def test_a_failed_budget_raise_rolls_back_the_credit_and_the_redemption(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, rooms, people, code = await _seed(db_engine)

    async def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected budget failure")

    monkeypatch.setattr(channel_budgets, "raise_channel_budget", fail)
    with pytest.raises(RuntimeError, match="injected"):
        await _redeem(factory, rooms[0], people[0])
    async with factory() as session:
        assert await tenant_ledger.get_balance(session, tenant_id=rooms[0].id) == Decimal("0")
        stored = await promo_codes.get_promo_code(session, code.id)
        assert stored is not None and stored.redeemed_count == 0
        assert await promo_codes.list_redemptions(session, promo_code_id=code.id) == []
