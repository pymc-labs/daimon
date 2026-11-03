"""The monthly limit per member, kept as spend allowances and enforced by the spend gate."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from daimon.core import allowance_limits
from daimon.core.spend_gate import SpendRequest, spend_gate
from daimon.core.stores import spend_allowances, tenant_ledger, usage_events
from daimon.testing.factories import make_monthly_limit, make_tenant
from mux.testing.drivers.anthropic.ma_models import ma_model_usage
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime.now(UTC)
START = datetime(2020, 1, 1, tzinfo=UTC)


async def _limit(session: AsyncSession, tenant_id: uuid.UUID, user: str = "u1") -> Decimal | None:
    return await allowance_limits.monthly_limit(
        session, tenant_id=tenant_id, platform_user_id=user, now=NOW
    )


async def test_the_default_is_one_monthly_allowance_for_everyone_set_in_place(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    first = await allowance_limits.set_default_limit(
        db_session, tenant_id=tenant.id, amount=Decimal("10"), now=START
    )
    again = await allowance_limits.set_default_limit(
        db_session, tenant_id=tenant.id, amount=Decimal("4"), now=NOW
    )
    rows = await spend_allowances.list_for_tenant(db_session, tenant_id=tenant.id)
    assert [(r.id, r.amount_usd) for r in rows] == [(first.id, Decimal("4"))], (
        "setting the default again changes the one allowance and adds none"
    )
    assert (again.subject_kind, again.subject_id, again.window, again.agent_id) == (
        "group",
        "everyone",
        "month",
        None,
    )


async def test_a_persons_own_limit_replaces_the_default_for_them(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    assert await _limit(db_session, tenant.id) is None, "no allowance: no limit"
    await make_monthly_limit(db_session, tenant=tenant, amount=Decimal("10"))
    await make_monthly_limit(db_session, tenant=tenant, user_id="u1", amount=Decimal("25"))
    assert await _limit(db_session, tenant.id, "u1") == Decimal("25"), "their own limit governs"
    assert await _limit(db_session, tenant.id, "u2") == Decimal("10"), (
        "everyone else keeps the default"
    )


async def test_only_monthly_allowances_for_every_agent_read_as_the_limit(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    for agent, window in (("a1", "month"), (None, "total")):
        await spend_allowances.create_allowance(
            db_session,
            tenant_id=tenant.id,
            subject_kind="group",
            subject_id="everyone",
            agent_id=agent,
            amount_usd=Decimal("3"),
            window=window,  # type: ignore[arg-type]
            starts_at=START,
        )
    assert await _limit(db_session, tenant.id) is None, (
        "an agent's allowance and a total one are not a monthly limit per member"
    )


async def test_the_gate_stops_a_person_at_their_limit_and_renews_it_next_month(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100"),
        reason="topup",
        idempotency_key=f"fund:{tenant.id}",
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant.id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-opus-4-7",
        model_usage=ma_model_usage(input_tokens=1_000_000, output_tokens=0),
        event_id="e1",
    )
    request = SpendRequest(
        tenant_id=tenant.id,
        platform_user_id="u1",
        platform="discord",
        channel_id=None,
        agent_id=None,
    )
    limit = await make_monthly_limit(db_session, tenant=tenant, amount=Decimal("6"))

    async def gate(now: datetime) -> str | None:
        return await spend_gate(db_session, request=request, markup=Decimal(1), now=now)

    assert await gate(NOW) is None, "$5 of $6 leaves room"
    await spend_allowances.set_amount(
        db_session, tenant_id=tenant.id, allowance_id=limit.id, amount_usd=Decimal("5")
    )
    assert await gate(NOW) == "allowance_exhausted", "spending exactly the limit is over it"
    next_month = datetime(NOW.year + (NOW.month == 12), NOW.month % 12 + 1, 1, 1, tzinfo=UTC)
    assert await gate(next_month) is None, "a new month starts with nothing used"
