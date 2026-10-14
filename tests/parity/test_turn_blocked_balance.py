"""Scenario (b): over-balance turn blocked pre-spend.

A tenant with a depleted balance (the default for a freshly-provisioned
tenant -- an empty ledger sums to `Decimal("0")`, and `is_over_balance`
treats `balance <= 0` as depleted) is blocked BEFORE `create_session` /
`run_turn` on both platforms, via the REAL platform entry point (D-02).
Asserts the per-driver expected copy (divergence principle, 02-CONTEXT
D-10 -- Discord and Slack copy are allowed to differ) and zero
`usage_events` / `tenant_ledger` rows.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.domain import Platform
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import build_turn_router
from .drivers.protocol import PlatformDriver, platform_ids


async def test_turn_blocked_when_over_balance_writes_no_usage_and_no_ledger_row(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, user_id, channel_id = platform_ids(
        driver.param_id, workspace=900002001, user=555000112, channel=100001
    )

    tenant = await make_tenant(
        db_session, platform=cast(Platform, driver.param_id), workspace_id=workspace_id
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
    )

    expected = driver.expected_blocked_text("balance")
    assert expected in posted, f"expected the over-balance copy {expected!r}, got: {posted}"

    usage_rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert usage_rows == [], "over-balance turn must write zero usage_events rows"

    ledger_rows = await tenant_ledger.list_for_tenant(db_session, tenant_id=tenant.id)
    assert ledger_rows == [], "over-balance turn must write zero tenant_ledger rows"
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == Decimal("0")
