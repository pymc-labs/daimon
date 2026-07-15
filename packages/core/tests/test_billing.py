"""Tests for daimon.core.tenant_balance — TOPUP-01 (D-14/D-15).

Covers is_over_balance (balance gate) and debit_amount (pure math).

- is_over_balance: returns True when balance <= 0, False when > 0
- is_over_balance: DM exemption (tenant_id=None -> False)
- is_over_balance: Stripe-independent (no billing_config needed)
- debit_amount: pure Decimal math, markup applied, None cost -> Decimal("0")
"""

from __future__ import annotations

from decimal import Decimal

from daimon.core import tenant_balance
from daimon.core.stores import tenant_ledger
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Pure unit tests — debit_amount (no DB)
# ---------------------------------------------------------------------------


def test_debit_amount_applies_markup_one_x() -> None:
    result = tenant_balance.debit_amount(0.0123, markup=Decimal("1.0"))
    assert result == Decimal("0.012300"), (
        "debit_amount with markup=1.0 should return cost unchanged, quantized to 6dp"
    )


def test_debit_amount_applies_markup_two_x() -> None:
    result = tenant_balance.debit_amount(0.01, markup=Decimal("2.0"))
    assert result == Decimal("0.020000"), "debit_amount with markup=2.0 should double the cost"


def test_debit_amount_none_cost_returns_zero() -> None:
    result = tenant_balance.debit_amount(None, markup=Decimal("1.0"))
    assert result == Decimal("0.000000"), (
        "None cost (unknown model) must produce Decimal('0') — no charge"
    )


def test_debit_amount_no_float_arithmetic_on_money() -> None:
    """Verify Decimal(str(cost)) conversion — not raw float arithmetic."""
    # 0.1 + 0.2 is a classic float precision problem; converting via str avoids it
    cost = 0.1 + 0.2  # ~0.30000000000000004 as float
    result = tenant_balance.debit_amount(cost, markup=Decimal("1.0"))
    # Decimal(str(0.1 + 0.2)) quantizes correctly; raw float multiplication would not
    assert result >= Decimal("0.299999") and result <= Decimal("0.300001"), (
        "debit_amount must use Decimal(str(cost)) to avoid float drift"
    )


# ---------------------------------------------------------------------------
# DB tests — is_over_balance (real Postgres)
# ---------------------------------------------------------------------------


async def test_is_over_balance_returns_true_when_balance_depleted(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    # Insert a negative entry to bring balance to 0
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("0"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.flush()
    result = await tenant_balance.is_over_balance(
        sessionmaker=db_session_factory, tenant_id=tenant.id
    )
    assert result is True, "balance=0 must be considered depleted (D-14: balance > 0 required)"


async def test_is_over_balance_returns_true_when_balance_negative(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    # Negative balance (over-debited)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("-5.00"),
        reason="turn_debit",
        idempotency_key="turn:s1:evt1",
    )
    await db_session.flush()
    result = await tenant_balance.is_over_balance(
        sessionmaker=db_session_factory, tenant_id=tenant.id
    )
    assert result is True, "negative balance must be considered depleted"


async def test_is_over_balance_returns_false_when_balance_positive(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("5.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.flush()
    result = await tenant_balance.is_over_balance(
        sessionmaker=db_session_factory, tenant_id=tenant.id
    )
    assert result is False, "positive balance must allow turns (D-14)"


async def test_is_over_balance_dm_exemption_returns_false_when_tenant_id_none(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await tenant_balance.is_over_balance(sessionmaker=db_session_factory, tenant_id=None)
    assert result is False, "DM (tenant_id=None) is exempt — mirror the cap DM exemption"


async def test_is_over_balance_stripe_independent_trial_credit_guild_can_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A guild with only trial credit and no Stripe config must still be allowed to turn.

    The balance gate takes no billing_config — it is Stripe-independent (RESEARCH Pitfall 4 / OQ#1).
    """
    tenant = await make_tenant(db_session)
    # Seed a trial credit — no Stripe payment needed
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.flush()
    # Call is_over_balance — it must NOT require billing_config to return False
    result = await tenant_balance.is_over_balance(
        sessionmaker=db_session_factory, tenant_id=tenant.id
    )
    assert result is False, (
        "trial-credit-only guild must be allowed (balance > 0) — gate is Stripe-independent"
    )
