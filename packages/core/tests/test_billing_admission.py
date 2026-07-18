"""Tests for daimon.core.billing — BILL-02.

Covers:
- No-cap-row exemption
- Threshold semantics (>= cap)
- Override > default precedence
- Calendar-month-UTC period boundary
- load_billing_config env contract

Note: the old DM-exemption test (guild_id=None short-circuit) has been removed.
DM admission gating is now the responsibility of the Discord adapter's
`should_process_message` in `daimon.adapters.discord.gating` — it returns False
when guild_id is None, so DMs never reach `is_over_cap`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core import billing
from daimon.core._models import UsageEvent
from daimon.core.stores import tenant_user_caps, usage_events
from daimon.testing.factories import make_tenant
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

_TEST_BILLING = billing.BillingConfig(
    secret_key=SecretStr("sk_test"),
    webhook_secret=SecretStr("whsec_test"),
    prices={10: "p10", 25: "p25", 50: "p50", 100: "p100"},
    success_url="http://test/success",
    cancel_url="http://test/cancel",
)


async def _record_1m_opus_tokens(
    session: AsyncSession,
    *,
    user_id: str,
    tenant_id: uuid.UUID,
    event_id: str,
) -> None:
    """Helper inlined per guideline:testing — explicit construction at call site.

    1M input tokens at claude-opus-4-7 input rate ($15/M) = $15.00.
    """
    await usage_events.record(
        session,
        tenant_id=tenant_id,
        platform_user_id=user_id,
        managed_session_id=f"s_{event_id}",
        model="claude-opus-4-7",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=1_000_000,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        event_id=event_id,
    )


async def test_is_over_cap_returns_false_when_no_cap_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    over = await billing.is_over_cap(
        billing_config=_TEST_BILLING,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        user_id="u1",
        now=datetime.now(UTC),
    )
    assert over is False, "no cap row = uncapped"


async def test_is_over_cap_returns_true_when_sum_meets_threshold(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    async with db_session_factory() as s, s.begin():
        await tenant_user_caps.set_default(
            s,
            tenant_id=tenant.id,
            amount=Decimal("15.00"),
        )
        await _record_1m_opus_tokens(s, user_id="u1", tenant_id=tenant.id, event_id="evt_1")
    over = await billing.is_over_cap(
        billing_config=_TEST_BILLING,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        user_id="u1",
        now=datetime.now(UTC),
    )
    assert over is True, "spent ($15.00) >= cap ($15.00) — admission denied"


async def test_is_over_cap_window_follows_injected_now(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The calendar-month window is derived from the injected ``now`` parameter,
    not the wall clock — proving the clock is injected (DI), testable without
    monkeypatching ``datetime.now``."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    async with db_session_factory() as s, s.begin():
        await tenant_user_caps.set_default(
            s,
            tenant_id=tenant.id,
            amount=Decimal("10.00"),
        )
        # $15 spent, stamped at the current wall-clock month.
        await _record_1m_opus_tokens(s, user_id="u1", tenant_id=tenant.id, event_id="evt_1")

    now = datetime.now(UTC)
    over_this_month = await billing.is_over_cap(
        billing_config=_TEST_BILLING,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        user_id="u1",
        now=now,
    )
    next_month = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
    over_next_month = await billing.is_over_cap(
        billing_config=_TEST_BILLING,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        user_id="u1",
        now=next_month,
    )
    assert over_this_month is True, "spend in the injected now's month counts toward the cap"
    assert over_next_month is False, (
        "a later-month now starts a fresh window that excludes this month's spend"
    )


async def test_is_over_cap_returns_false_when_under_threshold(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    async with db_session_factory() as s, s.begin():
        await tenant_user_caps.set_default(
            s,
            tenant_id=tenant.id,
            amount=Decimal("20.00"),
        )
        await _record_1m_opus_tokens(s, user_id="u1", tenant_id=tenant.id, event_id="evt_1")
    over = await billing.is_over_cap(
        billing_config=_TEST_BILLING,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        user_id="u1",
        now=datetime.now(UTC),
    )
    assert over is False, "spent ($15.00) < cap ($20.00) — admission allowed"


async def test_is_over_cap_uses_override_when_set(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    async with db_session_factory() as s, s.begin():
        # Default would allow ($20 > $15 spent), override pulls cap below spent.
        await tenant_user_caps.set_default(
            s,
            tenant_id=tenant.id,
            amount=Decimal("20.00"),
        )
        await tenant_user_caps.set_override(
            s,
            tenant_id=tenant.id,
            user_id="u1",
            amount=Decimal("10.00"),
        )
        await _record_1m_opus_tokens(s, user_id="u1", tenant_id=tenant.id, event_id="evt_1")
    over = await billing.is_over_cap(
        billing_config=_TEST_BILLING,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        user_id="u1",
        now=datetime.now(UTC),
    )
    assert over is True, "override ($10) takes precedence over default ($20); $15 >= $10"


async def test_is_over_cap_period_boundary_utc_first_of_month(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A row recorded before the current calendar-month-UTC start is excluded."""
    tenant = await make_tenant(db_session)
    async with db_session_factory() as s, s.begin():
        await tenant_user_caps.set_default(
            s,
            tenant_id=tenant.id,
            amount=Decimal("15.00"),
        )
        # Insert a row dated in the previous month via direct ORM.
        now = datetime.now(UTC)
        month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
        prev_month_dt = month_start - timedelta(days=1)
        s.add(
            UsageEvent(
                id=uuid.uuid4(),
                occurred_at=prev_month_dt,
                tenant_id=tenant.id,
                platform_user_id="u1",
                managed_session_id="s_old",
                model="claude-opus-4-7",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                event_id="evt_old",
            )
        )
        await s.flush()
    over = await billing.is_over_cap(
        billing_config=_TEST_BILLING,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        user_id="u1",
        now=datetime.now(UTC),
    )
    assert over is False, "rows from prior calendar month must be excluded (Pitfall 3)"


def test_load_billing_config_returns_none_when_partial_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial env (6 of 7 vars) returns None instead of raising."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_xxx")
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("STRIPE_PRICE_10_USD", "price_10")
    monkeypatch.setenv("STRIPE_PRICE_25_USD", "price_25")
    monkeypatch.setenv("STRIPE_PRICE_50_USD", "price_50")
    monkeypatch.setenv("STRIPE_PRICE_100_USD", "price_100")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")

    assert billing.load_billing_config() is None, "partial env should return None"


def test_load_billing_config_returns_none_when_no_stripe_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All Stripe vars absent returns None."""
    for key in (
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRICE_10_USD",
        "STRIPE_PRICE_25_USD",
        "STRIPE_PRICE_50_USD",
        "STRIPE_PRICE_100_USD",
        "MCP_PUBLIC_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    assert billing.load_billing_config() is None, "no STRIPE_* env should return None"


async def test_is_over_cap_returns_false_when_billing_config_none(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    result = await billing.is_over_cap(
        billing_config=None,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        user_id="u1",
        now=datetime.now(UTC),
    )
    assert result is False, "billing_config=None should allow all turns"


def test_load_billing_config_returns_config_when_env_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_xxx")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_xxx")
    monkeypatch.setenv("STRIPE_PRICE_10_USD", "price_10")
    monkeypatch.setenv("STRIPE_PRICE_25_USD", "price_25")
    monkeypatch.setenv("STRIPE_PRICE_50_USD", "price_50")
    monkeypatch.setenv("STRIPE_PRICE_100_USD", "price_100")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")

    cfg = billing.load_billing_config()
    assert cfg.secret_key.get_secret_value() == "sk_test_xxx"
    assert cfg.webhook_secret.get_secret_value() == "whsec_xxx"
    assert cfg.prices == {
        10: "price_10",
        25: "price_25",
        50: "price_50",
        100: "price_100",
    }
    assert cfg.success_url == "https://example.test/billing/success"
    assert cfg.cancel_url == "https://example.test/billing/cancel"
