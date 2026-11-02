"""Scenario (d): a turn blocked by the person's spend allowance, pre-spend.

A person whose allowance is used up is refused BEFORE `create_session` / `run_turn`
on every platform, through the real entry point, with the same plain words. The
workspace has credit and no cap, so the allowance is the only thing that stops it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from daimon.core.stores import spend_allowances, tenant_ledger, usage_events
from daimon.core.stores.domain import Platform
from daimon.testing.factories import make_tenant, make_usage_event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import build_turn_router
from .drivers.protocol import PlatformDriver, platform_ids


async def test_turn_blocked_when_the_allowance_is_used_up_writes_no_new_usage_or_ledger_row(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, user_id, channel_id = platform_ids(
        driver.param_id, workspace=900003101, user=555000213, channel=100102
    )

    tenant = await make_tenant(
        db_session, platform=cast(Platform, driver.param_id), workspace_id=workspace_id
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await spend_allowances.create_allowance(
        db_session,
        tenant_id=tenant.id,
        subject_kind="group",
        subject_id="everyone",
        agent_id=None,
        amount_usd=Decimal("1.00"),
        window="total",
        starts_at=datetime.now(UTC) - timedelta(days=1),
    )
    # 2,000,000 input tokens on claude-sonnet-4-6 ($3.00/1M) = $6.00, over the $1 allowance.
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
        billing_config=None,
    )

    expected = driver.expected_blocked_text("allowance")
    assert expected in posted, f"expected the allowance copy {expected!r}, got: {posted}"

    usage_rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert len(usage_rows) == 1, "a blocked turn writes no new usage row"
    ledger_rows = await tenant_ledger.list_for_tenant(db_session, tenant_id=tenant.id)
    assert [row.delta_usd for row in ledger_rows] == [Decimal("100.00")], (
        "a blocked turn writes no ledger debit"
    )
