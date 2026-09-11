"""Real-Postgres tests for the thread_participation store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.core.stores import thread_participation as store
from daimon.core.thread_participation import ParticipationMode, ParticipationScope
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

PLATFORM = "discord"
ON, OFF, DISABLED = ParticipationMode.ON, ParticipationMode.OFF, ParticipationMode.DISABLED


async def test_modes_read_every_tier_in_one_call_and_upsert(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    key = {"tenant_id": tenant.id, "platform": PLATFORM}
    read = {**key, "channel_id": "chan-1", "thread_id": "thr-1"}

    modes = await store.get_participation_modes(db_session, **read)
    assert (modes.workspace, modes.channel, modes.thread) == (None, None, None), (
        "an untouched cascade has no explicit setting at any tier"
    )

    await store.set_participation_mode(
        db_session, **key, scope=ParticipationScope.WORKSPACE, scope_id=None, mode=OFF
    )
    await store.set_participation_mode(
        db_session, **key, scope=ParticipationScope.CHANNEL, scope_id="chan-1", mode=DISABLED
    )
    await store.set_participation_mode(
        db_session, **key, scope=ParticipationScope.THREAD, scope_id="thr-1", mode=ON
    )
    await store.set_participation_mode(
        db_session, **key, scope=ParticipationScope.THREAD, scope_id="thr-other", mode=ON
    )
    modes = await store.get_participation_modes(db_session, **read)
    assert (modes.workspace, modes.channel, modes.thread) == (OFF, DISABLED, ON), (
        "one read returns this thread's tiers only -- another thread's row is not among them"
    )

    await store.set_participation_mode(
        db_session, **key, scope=ParticipationScope.THREAD, scope_id="thr-1", mode=OFF
    )
    modes = await store.get_participation_modes(db_session, **read)
    assert modes.thread is OFF, "second set overwrites"


async def test_clear_reports_whether_a_row_existed(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    key = {
        "tenant_id": tenant.id,
        "platform": PLATFORM,
        "scope": ParticipationScope.CHANNEL,
        "scope_id": "chan-1",
    }
    assert await store.clear_participation_mode(db_session, **key) is False, (
        "clearing a scope that was never set reports no row"
    )
    await store.set_participation_mode(db_session, **key, mode=ON)
    assert await store.clear_participation_mode(db_session, **key) is True, (
        "clearing an explicit setting reports the row it removed"
    )
    modes = await store.get_participation_modes(
        db_session, tenant_id=tenant.id, platform=PLATFORM, channel_id="chan-1", thread_id=None
    )
    assert modes.channel is None, "a cleared scope inherits again"


async def test_modes_are_tenant_scoped(db_session: AsyncSession) -> None:
    a = await make_tenant(db_session, workspace_id="guild-a")
    b = await make_tenant(db_session, workspace_id="guild-b")
    await store.set_participation_mode(
        db_session,
        tenant_id=a.id,
        platform=PLATFORM,
        scope=ParticipationScope.WORKSPACE,
        scope_id=None,
        mode=ON,
    )
    modes = await store.get_participation_modes(
        db_session, tenant_id=b.id, platform=PLATFORM, channel_id=None, thread_id=None
    )
    assert modes.workspace is None, "another tenant's workspace row must not leak"


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
