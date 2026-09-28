"""Persistent dispatch must keep ticking while routines run, without duplicate fires."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.scheduler import RoutineDispatcher, run_one_tick
from daimon.core.stores.domain import CatchUpPolicy, RoutineRow
from daimon.core.stores.routines import create_routine, get_routine, update_routine
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.fixture
def db_session_factory(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
) -> async_sessionmaker[AsyncSession]:
    """Concurrent fire finalizers need independent connections, as in production.

    db_session triggers schema cleanup; these tests commit setup before dispatch.
    """
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


class NoCaps:
    async def is_over_cap(self, tenant_id: uuid.UUID, user_id: str) -> bool:
        return False


async def seed(
    session: AsyncSession, *, due: datetime, policy: CatchUpPolicy = "skip"
) -> RoutineRow:
    tenant = await make_tenant(session)
    row = await create_routine(
        session,
        tenant_id=tenant.id,
        created_by_user_id=None,
        agent_id="agent",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="run",
        next_fire_at=due,
        catch_up_policy=policy,
    )
    await session.commit()
    return row


async def test_slow_routine_does_not_block_next_tick_or_overlap_itself(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slow = await seed(db_session, due=NOW - timedelta(minutes=1), policy="run-once")
    finished_clock = NOW
    dispatcher = RoutineDispatcher(2, clock=lambda: finished_clock)
    release, started, fast_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    fired = []

    async def fire(row: RoutineRow) -> None:
        fired.append(row.id)
        if row.id == slow.id:
            started.set()
            await release.wait()
        else:
            fast_done.set()

    async def tick(now: datetime) -> None:
        await run_one_tick(
            now=now,
            sm=db_session_factory,
            caps=NoCaps(),
            fire=fire,
            max_age=timedelta(minutes=15),
            max_concurrent_fires=2,
            dispatch_timeout_s=60,
            dispatcher=dispatcher,
        )

    try:
        await asyncio.wait_for(tick(NOW), timeout=10)
        await asyncio.wait_for(started.wait(), timeout=10)
        later = NOW + timedelta(days=1)
        fast = await seed(db_session, due=later - timedelta(minutes=1))
        await asyncio.wait_for(tick(later), timeout=10)
        await asyncio.wait_for(fast_done.wait(), timeout=10)
        assert fired.count(slow.id) == 1
        assert fired.count(fast.id) == 1
        assert slow.id in dispatcher.in_flight_ids
        async with db_session_factory() as session:
            row = await get_routine(session, slow.id, tenant_id=slow.tenant_id)
        assert row is not None
        assert row.last_skip_reason == "in_flight"
        assert row.last_skipped_from == NOW + timedelta(minutes=1)
        assert row.last_skipped_until == later

        # Completing an old run must preserve a schedule edited while it ran.
        edited_next = later + timedelta(minutes=1)
        finished_clock = later + timedelta(minutes=2)
        async with db_session_factory() as session, session.begin():
            await update_routine(
                session,
                slow.id,
                tenant_id=slow.tenant_id,
                cron_expr="0 */5 * * *",
                next_fire_at=edited_next,
            )
        # A tick during the old run must also preserve the new, now-due schedule.
        await tick(finished_clock)
        async with db_session_factory() as session:
            during = await get_routine(session, slow.id, tenant_id=slow.tenant_id)
        assert during is not None and during.next_fire_at == edited_next
        release.set()
        await dispatcher.drain()
        async with db_session_factory() as session:
            edited = await get_routine(session, slow.id, tenant_id=slow.tenant_id)
        assert edited is not None and edited.next_fire_at == edited_next
    finally:
        release.set()
        await dispatcher.close()


@pytest.mark.parametrize("policy,expected_fires", [("skip", 0), ("run-once", 1)])
async def test_missed_slots_skip_or_coalesce_once(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    policy: CatchUpPolicy,
    expected_fires: int,
) -> None:
    due = NOW - timedelta(days=2)
    routine = await seed(db_session, due=due, policy=policy)
    fired = []

    async def fire(row: RoutineRow) -> None:
        fired.append(row.id)

    for _ in range(2):
        await run_one_tick(
            now=NOW,
            sm=db_session_factory,
            caps=NoCaps(),
            fire=fire,
            max_age=timedelta(minutes=15),
            max_concurrent_fires=2,
            dispatch_timeout_s=60,
            wait_for_completion=True,
        )
    assert fired == [routine.id] * expected_fires
    async with db_session_factory() as session:
        row = await get_routine(session, routine.id, tenant_id=routine.tenant_id)
    assert row is not None and row.next_fire_at is not None and row.next_fire_at > NOW
    if policy == "skip":
        assert row.last_skip_reason == "stale"
        assert row.last_skipped_from == due
        assert row.last_skipped_until == NOW
    else:
        assert row.last_fired_at == NOW


async def test_capacity_remains_bounded_across_ticks_and_shutdown_joins_fires(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await seed(db_session, due=NOW - timedelta(minutes=2))
    pending = await seed(db_session, due=NOW - timedelta(minutes=1))
    dispatcher = RoutineDispatcher(1, clock=lambda: NOW)
    started, release = asyncio.Event(), asyncio.Event()
    fired = []

    async def fire(row: RoutineRow) -> None:
        fired.append(row.id)
        started.set()
        await release.wait()

    async def tick() -> None:
        await run_one_tick(
            now=NOW,
            sm=db_session_factory,
            caps=NoCaps(),
            fire=fire,
            max_age=timedelta(minutes=15),
            max_concurrent_fires=1,
            dispatch_timeout_s=60,
            dispatcher=dispatcher,
        )

    try:
        await tick()
        await asyncio.wait_for(started.wait(), timeout=10)
        await tick()
        assert fired == [first.id]
        async with db_session_factory() as session:
            row = await get_routine(session, pending.id, tenant_id=pending.tenant_id)
        assert row is not None and row.last_fired_at is None
        await dispatcher.close()
        assert not dispatcher.in_flight_ids
        async with db_session_factory() as session:
            stopped = await get_routine(session, first.id, tenant_id=first.tenant_id)
        assert stopped is not None and stopped.last_error == "scheduler_shutdown"
        release.set()
        await tick()
        await dispatcher.drain()
        assert fired == [first.id, pending.id]
    finally:
        release.set()
        await dispatcher.close()


async def test_slots_due_between_ticks_are_skipped_when_fire_completes(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    routine = await seed(db_session, due=NOW - timedelta(minutes=1), policy="run-once")
    finished_at = NOW + timedelta(minutes=3)
    dispatcher = RoutineDispatcher(1, clock=lambda: finished_at)
    fired = []

    async def fire(row: RoutineRow) -> None:
        fired.append(row.id)

    await run_one_tick(
        now=NOW,
        sm=db_session_factory,
        caps=NoCaps(),
        fire=fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=1,
        dispatch_timeout_s=60,
        dispatcher=dispatcher,
    )
    await dispatcher.drain()
    await run_one_tick(
        now=finished_at,
        sm=db_session_factory,
        caps=NoCaps(),
        fire=fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=1,
        dispatch_timeout_s=60,
        dispatcher=dispatcher,
    )
    await dispatcher.drain()
    assert fired == [routine.id]
    async with db_session_factory() as session:
        row = await get_routine(session, routine.id, tenant_id=routine.tenant_id)
    assert row is not None and row.last_skip_reason == "in_flight"
    assert row.last_skipped_from == NOW + timedelta(minutes=1)
    assert row.last_skipped_until == finished_at
    await dispatcher.close()


async def test_tick_returns_dispatcher_without_waiting_for_a_fire(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed(db_session, due=NOW - timedelta(minutes=1))
    started, release = asyncio.Event(), asyncio.Event()

    async def fire(row: RoutineRow) -> None:
        started.set()
        await release.wait()

    dispatcher = await asyncio.wait_for(
        run_one_tick(
            now=NOW,
            sm=db_session_factory,
            caps=NoCaps(),
            fire=fire,
            max_age=timedelta(minutes=15),
            max_concurrent_fires=1,
            dispatch_timeout_s=60,
        ),
        timeout=10,
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        assert len(dispatcher.in_flight_ids) == 1
    finally:
        release.set()
        await dispatcher.close()
