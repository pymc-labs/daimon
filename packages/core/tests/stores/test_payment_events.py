"""Tests for daimon.core.stores.payment_events — BILL-01."""

from __future__ import annotations

from decimal import Decimal

import pytest
from daimon.core.stores import payment_events
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def test_upsert_for_dedup_inserts_new_row(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    row = await payment_events.upsert_for_dedup(
        db_session,
        event_id="evt_stripe_1",
        amount_usd=Decimal("10.00"),
        source="stripe",
        tenant_id=tenant.id,
    )
    assert row.id == "evt_stripe_1"
    assert row.amount_usd == Decimal("10.00")
    assert row.tenant_id == tenant.id, "row must carry the supplied tenant_id"
    assert row.credited_at is None, "fresh row must have credited_at NULL"


async def test_upsert_for_dedup_returns_existing_on_replay(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    first = await payment_events.upsert_for_dedup(
        db_session,
        event_id="evt_stripe_1",
        amount_usd=Decimal("10.00"),
        source="stripe",
        tenant_id=tenant.id,
    )
    # Replay with a different amount must NOT overwrite — DO NOTHING.
    second = await payment_events.upsert_for_dedup(
        db_session,
        event_id="evt_stripe_1",
        amount_usd=Decimal("9999.00"),
        source="stripe",
        tenant_id=tenant.id,
    )
    assert second.id == first.id
    assert second.amount_usd == Decimal("10.00"), (
        "ON CONFLICT DO NOTHING must preserve the original amount on replay"
    )


async def test_try_claim_credit_returns_true_first_call_false_second(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await payment_events.upsert_for_dedup(
        db_session,
        event_id="evt_stripe_1",
        amount_usd=Decimal("10.00"),
        source="stripe",
        tenant_id=tenant.id,
    )
    first = await payment_events.try_claim_credit(db_session, "evt_stripe_1")
    assert first is True, "first claim should win the compare-and-set"
    second = await payment_events.try_claim_credit(db_session, "evt_stripe_1")
    assert second is False, "second claim must fail — credited_at IS NULL no longer matches"


async def test_unclaim_credit_resets_credited_at_to_null(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await payment_events.upsert_for_dedup(
        db_session,
        event_id="evt_stripe_1",
        amount_usd=Decimal("10.00"),
        source="stripe",
        tenant_id=tenant.id,
    )
    await payment_events.try_claim_credit(db_session, "evt_stripe_1")
    await payment_events.unclaim_credit(db_session, "evt_stripe_1")
    row = await payment_events.get(db_session, "evt_stripe_1")
    assert row is not None
    assert row.credited_at is None, "unclaim_credit must reset credited_at to NULL"
    # And try_claim_credit can now succeed again.
    again = await payment_events.try_claim_credit(db_session, "evt_stripe_1")
    assert again is True, "after unclaim, the slot is available again"


async def test_get_returns_none_for_missing_event_id(
    db_session: AsyncSession,
) -> None:
    row = await payment_events.get(db_session, "evt_missing")
    assert row is None, "missing event_id should return None, not raise"
