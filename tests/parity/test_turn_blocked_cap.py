"""Scenario (c): over-cap turn blocked pre-spend.

A user who has already spent at-or-above their effective monthly cap is
blocked BEFORE `create_session` / `run_turn` on both platforms, via the
REAL platform entry point (D-02). Asserts the per-driver expected copy and
that the blocked turn writes no NEW `usage_events` row or `tenant_ledger`
debit (the pre-seeded usage_event establishing the cap breach is untouched).
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from daimon.core.billing import BillingConfig
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.domain import Platform
from daimon.testing.factories import make_tenant, make_tenant_user_cap, make_usage_event
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import build_turn_router
from .drivers.protocol import PlatformDriver, platform_ids

_BILLING_CONFIG = BillingConfig(
    secret_key=SecretStr("sk_test_parity"),
    webhook_secret=SecretStr("whsec_test_parity"),
    prices={10: "price_10", 25: "price_25", 50: "price_50", 100: "price_100"},
    success_url="https://example.test/success",
    cancel_url="https://example.test/cancel",
)


async def test_turn_blocked_when_over_cap_writes_no_new_usage_or_ledger_row(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, user_id, channel_id = platform_ids(
        driver.param_id, workspace=900003001, user=555000113, channel=100002
    )

    tenant = await make_tenant(
        db_session, platform=cast(Platform, driver.param_id), workspace_id=workspace_id
    )
    # Balance must NOT be the blocker here -- credit it well past depleted.
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    # Tenant-wide default cap of $5.
    await make_tenant_user_cap(db_session, tenant=tenant, amount=Decimal("5"))
    # 2,000,000 input tokens on claude-sonnet-4-6 ($3.00/1M input) = $6.00 spent
    # this month -- already at/above the $5 cap before the turn even starts.
    await make_usage_event(
        db_session,
        tenant=tenant,
        platform_user_id=user_id,
        model="claude-sonnet-4-6",
        input_tokens=2_000_000,
        output_tokens=0,
    )
    await db_session.commit()

    router = build_turn_router(str(tenant.id))
    posted = await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        text="hello",
        billing_config=_BILLING_CONFIG,
    )

    expected = driver.expected_blocked_text("cap")
    assert expected in posted, f"expected the over-cap copy {expected!r}, got: {posted}"

    usage_rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert len(usage_rows) == 1, (
        "over-cap turn must write no NEW usage_events row (only the pre-seeded breach row)"
    )

    ledger_rows = await tenant_ledger.list_for_tenant(db_session, tenant_id=tenant.id)
    debit_rows = [row for row in ledger_rows if row.delta_usd < 0]
    assert debit_rows == [], "over-cap turn must write zero tenant_ledger debits"
