"""Real-Postgres tests for the progress-plan scope store."""

from __future__ import annotations

from daimon.core.scope_cascade import CascadeMode, CascadeScope
from daimon.core.stores import progress_plans as store
from daimon.core.stores.thread_participation import get_participation_modes
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

ON, OFF, DISABLED = CascadeMode.ON, CascadeMode.OFF, CascadeMode.DISABLED


async def test_modes_read_this_threads_tiers_and_upsert(db_session: AsyncSession) -> None:
    """One read returns the three tiers for this thread; a second write overwrites."""
    tenant = await make_tenant(db_session)
    key = {"tenant_id": tenant.id, "platform": "slack"}
    read = {**key, "channel_id": "C1", "thread_id": "C1:1.2"}

    await store.set_progress_plan_mode(
        db_session, **key, scope=CascadeScope.WORKSPACE, scope_id=None, mode=OFF
    )
    await store.set_progress_plan_mode(
        db_session, **key, scope=CascadeScope.CHANNEL, scope_id="C1", mode=ON
    )
    await store.set_progress_plan_mode(
        db_session, **key, scope=CascadeScope.THREAD, scope_id="C2:1.2", mode=OFF
    )
    modes = await store.get_progress_plan_modes(db_session, **read)
    assert (modes.workspace, modes.channel, modes.thread) == (OFF, ON, None), (
        "a thread with the same timestamp in another channel is not this thread"
    )

    await store.set_progress_plan_mode(
        db_session, **key, scope=CascadeScope.CHANNEL, scope_id="C1", mode=DISABLED
    )
    modes = await store.get_progress_plan_modes(db_session, **read)
    assert modes.channel is DISABLED, "a second set overwrites"


async def test_progress_plans_and_participation_keep_separate_rows(
    db_session: AsyncSession,
) -> None:
    """The two switches share a cascade, never a table."""
    tenant = await make_tenant(db_session)
    await store.set_progress_plan_mode(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        scope=CascadeScope.CHANNEL,
        scope_id="chan-1",
        mode=ON,
    )
    participation = await get_participation_modes(
        db_session, tenant_id=tenant.id, platform="discord", channel_id="chan-1", thread_id="t"
    )
    assert participation.channel is None, "a progress-plan row must not read as participation"


async def test_clear_reports_whether_a_row_existed(db_session: AsyncSession) -> None:
    """Clearing returns the scope to inheriting and says whether there was a value."""
    tenant = await make_tenant(db_session)
    key = {
        "tenant_id": tenant.id,
        "platform": "teams",
        "scope": CascadeScope.THREAD,
        "scope_id": "19:a;messageid=1",
    }
    assert await store.clear_progress_plan_mode(db_session, **key) is False, (
        "clearing an unset scope reports no row"
    )
    await store.set_progress_plan_mode(db_session, **key, mode=ON)
    assert await store.clear_progress_plan_mode(db_session, **key) is True, (
        "clearing a set scope reports the row"
    )
