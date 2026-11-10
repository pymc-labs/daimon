"""Real-Postgres tests for the auto-response ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.core.stores import thread_participation as store
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

PLATFORM = "discord"


async def test_ledger_counts_only_this_thread_within_the_window(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    now = datetime.now(UTC)
    key = {"tenant_id": tenant.id, "platform": PLATFORM, "thread_id": "thr-1"}
    for minutes, mid in ((90, "old"), (30, "mid"), (1, "new")):
        await store.record_auto_response(
            db_session, **key, message_id=mid, created_at=now - timedelta(minutes=minutes)
        )
    await store.record_auto_response(
        db_session, tenant_id=tenant.id, platform=PLATFORM, thread_id="thr-2", message_id="x"
    )
    count = await store.count_auto_responses_since(
        db_session, **key, since=now - timedelta(hours=1)
    )
    assert count == 2, "only this thread's rows inside the window count against the cap"
