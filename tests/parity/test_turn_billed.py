"""Scenario (a): turn -> billed.

A non-blocked turn dispatched via the REAL platform entry point (D-02) --
`bot.on_message` for Discord, `SlackApp._handle_app_mention` for Slack --
driving the REAL `run_turn` SSE turn driver (D-01, never patched) writes a
`usage_events` row AND a `tenant_ledger` debit, on BOTH platforms. Reverting
Phase-02's `usage_record` wiring into `run_turn` (bot.py:1107-1116 /
app.py:1148-1157) would make this test fail on the affected platform -- the
exact regression class this suite exists to catch.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from daimon.core.continuity.messages import render_unexpected_loss
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.domain import Platform
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import build_turn_router
from .drivers.protocol import PlatformDriver, platform_ids


async def test_turn_billed_when_unblocked_writes_usage_event_and_ledger_debit(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, user_id, channel_id = platform_ids(
        driver.param_id, workspace=900001001, user=555000111, channel=100000
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
    assert posted, f"expected the agent's reply to be posted somewhere, got: {posted}"

    # Issue 4 (staging QA, 2026-09-13): an ordinary turn (no dead-session
    # recovery) must never post the unexpected-loss notice, on either
    # platform.
    assert render_unexpected_loss("history") not in posted
    assert render_unexpected_loss("transcript") not in posted

    usage_rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert len(usage_rows) == 1, "unblocked turn must write exactly one usage_events row"
    assert usage_rows[0].managed_session_id == "sess_parity_test"
    assert usage_rows[0].model == "claude-sonnet-4-6"

    ledger_rows = await tenant_ledger.list_for_tenant(db_session, tenant_id=tenant.id)
    debit_rows = [row for row in ledger_rows if row.delta_usd < 0]
    assert len(debit_rows) == 1, "unblocked turn must write exactly one tenant_ledger debit"
    assert debit_rows[0].delta_usd == Decimal("-0.001050")
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == Decimal("99.998950")
