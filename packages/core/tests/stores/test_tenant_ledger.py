"""Tests for daimon.core.stores.tenant_ledger — TOPUP-01."""

from __future__ import annotations

from decimal import Decimal

import pytest
from daimon.core.stores import tenant_ledger
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def test_insert_entry_fresh_key_returns_true_and_creates_one_row(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    inserted = await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="topup",
        idempotency_key="topup:evt_001",
    )
    assert inserted is True, "insert_entry with a fresh idempotency_key must return True"
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant.id)
    assert balance == Decimal("10.00"), "balance must reflect the inserted row"


async def test_insert_entry_duplicate_key_returns_false_and_leaves_one_row(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    first = await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="topup",
        idempotency_key="topup:evt_dupe",
    )
    second = await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="topup",
        idempotency_key="topup:evt_dupe",
    )
    assert first is True, "first insert must return True (new row)"
    assert second is False, "second insert with same idempotency_key must return False (DO NOTHING)"
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant.id)
    assert balance == Decimal("10.00"), "only one row must exist — ON CONFLICT DO NOTHING"


async def test_get_balance_sums_delta_usd_across_all_rows(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="topup",
        idempotency_key="topup:a",
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("5.50"),
        reason="topup",
        idempotency_key="topup:b",
    )
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant.id)
    assert balance == Decimal("15.50"), "get_balance must return SUM(delta_usd) across all rows"


async def test_get_balance_returns_zero_when_ledger_is_empty(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant.id)
    assert balance == Decimal("0"), "empty ledger must return Decimal('0'), not None"


async def test_negative_delta_usd_drives_balance_negative(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("5.00"),
        reason="topup",
        idempotency_key="topup:seed",
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("-10.00"),
        reason="charge.refunded",
        idempotency_key="clawback:evt_refund",
    )
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant.id)
    assert balance == Decimal("-5.00"), (
        "negative delta_usd (debit/clawback) must be allowed and drive balance negative"
    )


async def test_get_balance_isolates_tenants(
    db_session: AsyncSession,
) -> None:
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant_a.id,
        delta_usd=Decimal("20.00"),
        reason="topup",
        idempotency_key="topup:a1",
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant_b.id,
        delta_usd=Decimal("7.00"),
        reason="topup",
        idempotency_key="topup:b1",
    )
    balance_a = await tenant_ledger.get_balance(db_session, tenant_id=tenant_a.id)
    balance_b = await tenant_ledger.get_balance(db_session, tenant_id=tenant_b.id)
    assert balance_a == Decimal("20.00"), "tenant A balance must not include tenant B rows"
    assert balance_b == Decimal("7.00"), "tenant B balance must not include tenant A rows"


async def test_get_by_payment_intent_returns_topup_credit_row(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("50.00"),
        reason="topup",
        idempotency_key="topup:evt_pi_abc",
        payment_intent="pi_abc123",
    )
    row = await tenant_ledger.get_by_payment_intent(db_session, payment_intent="pi_abc123")
    assert row is not None, "get_by_payment_intent must return the matching topup row"
    assert row.tenant_id == tenant.id, "row must belong to the correct tenant"
    assert row.delta_usd == Decimal("50.00"), "row must carry the original credit amount"
    assert row.payment_intent == "pi_abc123", "row must carry the payment_intent"


async def test_get_by_payment_intent_returns_none_when_no_match(
    db_session: AsyncSession,
) -> None:
    row = await tenant_ledger.get_by_payment_intent(db_session, payment_intent="pi_does_not_exist")
    assert row is None, "get_by_payment_intent must return None when no matching row exists"


async def test_get_clawed_back_total_returns_zero_when_no_clawbacks(
    db_session: AsyncSession,
) -> None:
    total = await tenant_ledger.get_clawed_back_total(db_session, payment_intent="pi_none")
    assert total == Decimal("0"), "no clawback rows -> Decimal('0')"


async def test_get_clawed_back_total_excludes_topup_credit_row(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="topup",
        idempotency_key="topup:pi_excl",
        payment_intent="pi_excl",
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("-3.00"),
        reason="charge.refunded",
        idempotency_key="clawback:pi_excl:evt_r1",
        payment_intent="pi_excl",
    )
    total = await tenant_ledger.get_clawed_back_total(db_session, payment_intent="pi_excl")
    assert total == Decimal("3.00"), (
        "get_clawed_back_total must return the positive sum of negative rows, excluding the topup"
    )


async def test_get_clawed_back_total_sums_multiple_clawback_rows(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("20.00"),
        reason="topup",
        idempotency_key="topup:pi_sum",
        payment_intent="pi_sum",
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("-3.00"),
        reason="charge.refunded",
        idempotency_key="clawback:pi_sum:evt_r1",
        payment_intent="pi_sum",
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("-4.00"),
        reason="charge.refunded",
        idempotency_key="clawback:pi_sum:evt_r2",
        payment_intent="pi_sum",
    )
    total = await tenant_ledger.get_clawed_back_total(db_session, payment_intent="pi_sum")
    assert total == Decimal("7.00"), "multiple clawback rows must sum to a positive Decimal('7')"


async def test_get_clawed_back_total_scoped_by_payment_intent(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("-5.00"),
        reason="charge.refunded",
        idempotency_key="clawback:pi_a:evt_r1",
        payment_intent="pi_a",
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("-9.00"),
        reason="charge.refunded",
        idempotency_key="clawback:pi_b:evt_r1",
        payment_intent="pi_b",
    )
    total_a = await tenant_ledger.get_clawed_back_total(db_session, payment_intent="pi_a")
    assert total_a == Decimal("5.00"), "clawbacks on a different payment_intent must not be counted"
