"""The durable wake queue (`daimon.core.continuity.wakes`) against real Postgres.

The properties that matter: a wake outlives the process that queued it and
runs exactly once; a lease that expires before the turn started is retried; a
lease that expires after it started is settled, never re-run.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.continuity.continuation import (
    ContinuationRequest,
    claim_continuation,
    record_continuation,
)
from daimon.core.continuity.wakes import (
    WAKE_CLAIM_LEASE,
    WAKE_MAX_ATTEMPTS,
    WAKE_RUN_LEASE,
    WAKE_UNAVAILABLE_DELAY,
    WakeThread,
    abandon_interrupted_wakes,
    cancel_wake,
    claim_wake,
    enqueue_wake,
    list_due_wake_threads,
    poll_wakes_once,
    release_wake,
    settle_wake,
    skip_thread_wakes,
    start_wake,
)
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.task_continuations import get_continuation, list_pending_continuations
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _request(tenant_id: uuid.UUID, *, thread_id: str = "thread-1") -> ContinuationRequest:
    return ContinuationRequest(
        tenant_id=tenant_id,
        platform="discord",
        parent_channel_id="channel-1",
        thread_id=thread_id,
        requester_account_id=uuid.uuid4(),
        requester_external_user_id="discord-user-1",
        target_ma_agent_id="agent_stats",
        target_name="stats-bot",
        requested_work="check whether the nightly build went green",
        reason="task_handoff",
        idempotency_key=uuid.uuid4(),
    )


async def _tenant(sessionmaker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessionmaker.begin() as session:
        return (await make_tenant(session)).id


async def _read(
    sessionmaker: async_sessionmaker[AsyncSession], key: uuid.UUID
) -> TaskContinuationRow:
    async with sessionmaker() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None
    return row


async def test_a_wake_is_not_claimable_or_polled_before_it_is_due(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request(await _tenant(db_session_factory))
    await enqueue_wake(db_session_factory, request, available_at=_NOW + timedelta(hours=2))

    assert await list_due_wake_threads(db_session_factory, platform="discord", now=_NOW) == []
    assert (
        await claim_wake(db_session_factory, idempotency_key=request.idempotency_key, now=_NOW)
        is None
    )

    due = _NOW + timedelta(hours=2)
    threads = await list_due_wake_threads(db_session_factory, platform="discord", now=due)
    assert [t.thread_id for t in threads] == ["thread-1"]
    assert await claim_wake(db_session_factory, idempotency_key=request.idempotency_key, now=due)


async def test_a_handoff_without_available_at_is_left_to_the_next_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The poller never opens a thread for a plain handoff row; its behaviour is unchanged."""
    request = _request(await _tenant(db_session_factory))
    await record_continuation(db_session_factory, request)

    assert await list_due_wake_threads(db_session_factory, platform="discord", now=_NOW) == []


async def test_an_expired_unstarted_claim_is_retried_and_a_started_one_is_not(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(db_session_factory)
    unstarted = _request(tenant_id, thread_id="thread-a")
    started = _request(tenant_id, thread_id="thread-b")
    for request in (unstarted, started):
        await enqueue_wake(db_session_factory, request, available_at=_NOW)

    first = await claim_wake(
        db_session_factory, idempotency_key=unstarted.idempotency_key, now=_NOW
    )
    assert first is not None
    other = await claim_wake(db_session_factory, idempotency_key=started.idempotency_key, now=_NOW)
    assert other is not None
    assert await start_wake(db_session_factory, other, now=_NOW)

    live = _NOW + WAKE_CLAIM_LEASE - timedelta(seconds=1)
    assert (
        await claim_wake(db_session_factory, idempotency_key=unstarted.idempotency_key, now=live)
        is None
    )

    expired = _NOW + WAKE_CLAIM_LEASE + timedelta(seconds=1)
    takeover = await claim_wake(
        db_session_factory, idempotency_key=unstarted.idempotency_key, now=expired
    )
    assert takeover is not None and takeover.owner != first.owner
    assert takeover.attempts == 2

    # The dead owner comes back: every write it tries is refused.
    assert not await start_wake(db_session_factory, first, now=expired)
    assert not await settle_wake(db_session_factory, first, status="delivered", now=expired)

    after_run_lease = _NOW + WAKE_RUN_LEASE + timedelta(seconds=1)
    assert (
        await claim_wake(
            db_session_factory, idempotency_key=started.idempotency_key, now=after_run_lease
        )
        is None
    ), "a started claim is never taken over, however stale"
    abandoned = await abandon_interrupted_wakes(
        db_session_factory, platform="discord", now=after_run_lease
    )
    assert [row.idempotency_key for row in abandoned] == [started.idempotency_key]
    row = await _read(db_session_factory, started.idempotency_key)
    assert row.status == "skipped" and row.skip_reason == "interrupted"


async def test_a_release_refunds_its_claim_so_waiting_never_exhausts_the_row(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request(await _tenant(db_session_factory))
    await enqueue_wake(db_session_factory, request, available_at=_NOW)

    at = _NOW
    for _ in range(WAKE_MAX_ATTEMPTS * 3):
        claim = await claim_wake(
            db_session_factory, idempotency_key=request.idempotency_key, now=at
        )
        assert claim is not None and claim.attempts == 1
        assert await start_wake(db_session_factory, claim, now=at)
        at = at + timedelta(minutes=1)
        assert await release_wake(db_session_factory, claim, retry_at=at)
        row = await _read(db_session_factory, request.idempotency_key)
        assert row.status == "pending" and row.attempts == 0 and row.started_at is None
        assert row.available_at == at


async def test_a_handoff_release_without_retry_at_keeps_waiting_for_a_turn_tail(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request(await _tenant(db_session_factory))
    await record_continuation(db_session_factory, request)
    claim = await claim_wake(db_session_factory, idempotency_key=request.idempotency_key, now=_NOW)
    assert claim is not None

    assert await release_wake(db_session_factory, claim, retry_at=None)
    row = await _read(db_session_factory, request.idempotency_key)
    assert row.status == "pending" and row.available_at is None
    assert await list_due_wake_threads(db_session_factory, platform="discord", now=_NOW) == []


async def test_crashed_claims_exhaust_the_row_after_max_attempts(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request(await _tenant(db_session_factory))
    await enqueue_wake(db_session_factory, request, available_at=_NOW)

    at = _NOW
    for attempt in range(1, WAKE_MAX_ATTEMPTS + 1):
        claim = await claim_wake(
            db_session_factory, idempotency_key=request.idempotency_key, now=at
        )
        assert claim is not None and claim.attempts == attempt
        at = at + WAKE_CLAIM_LEASE + timedelta(seconds=1)  # the holder died; lease runs out
    assert (
        await claim_wake(db_session_factory, idempotency_key=request.idempotency_key, now=at)
        is None
    )
    await abandon_interrupted_wakes(db_session_factory, platform="discord", now=at)
    row = await _read(db_session_factory, request.idempotency_key)
    assert row.status == "skipped" and row.skip_reason == "attempts_exhausted"


async def test_legacy_claim_and_list_never_hand_out_a_future_wake(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An adapter still on the pre-queue API cannot run a scheduled wake early."""
    tenant_id = await _tenant(db_session_factory)
    request = _request(tenant_id)
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    await enqueue_wake(db_session_factory, request, available_at=tomorrow)

    async with db_session_factory() as session:
        listed = await list_pending_continuations(
            session, tenant_id=tenant_id, platform="discord", thread_id="thread-1"
        )
    assert listed == []
    assert not await claim_continuation(
        db_session_factory, idempotency_key=request.idempotency_key, now=datetime.now(UTC)
    )
    assert await claim_continuation(
        db_session_factory, idempotency_key=request.idempotency_key, now=tomorrow
    )


async def test_a_cancelled_wake_can_never_be_claimed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(db_session_factory)
    request = _request(tenant_id)
    await enqueue_wake(db_session_factory, request, available_at=_NOW)

    assert not await cancel_wake(
        db_session_factory, tenant_id=uuid.uuid4(), idempotency_key=request.idempotency_key
    ), "another tenant cannot cancel it"
    assert await cancel_wake(
        db_session_factory, tenant_id=tenant_id, idempotency_key=request.idempotency_key
    )
    assert (
        await claim_wake(db_session_factory, idempotency_key=request.idempotency_key, now=_NOW)
        is None
    )
    assert await list_due_wake_threads(db_session_factory, platform="discord", now=_NOW) == []
    assert (await _read(db_session_factory, request.idempotency_key)).status == "cancelled"


async def test_a_claimed_wake_cannot_be_cancelled(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(db_session_factory)
    request = _request(tenant_id)
    await enqueue_wake(db_session_factory, request, available_at=_NOW)
    assert await claim_wake(db_session_factory, idempotency_key=request.idempotency_key, now=_NOW)

    assert not await cancel_wake(
        db_session_factory, tenant_id=tenant_id, idempotency_key=request.idempotency_key
    )


async def test_skip_thread_wakes_settles_every_due_wake_in_the_thread(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(db_session_factory)
    due, later = _request(tenant_id), _request(tenant_id)
    await enqueue_wake(db_session_factory, due, available_at=_NOW)
    await enqueue_wake(db_session_factory, later, available_at=_NOW + timedelta(days=1))
    thread = WakeThread(
        tenant_id=tenant_id,
        platform="discord",
        parent_channel_id="channel-1",
        thread_id="thread-1",
        requester_account_id=due.requester_account_id,
    )

    assert (
        await skip_thread_wakes(
            db_session_factory, thread=thread, reason="thread_unavailable", now=_NOW
        )
        == 1
    )
    assert (
        await _read(db_session_factory, due.idempotency_key)
    ).skip_reason == "thread_unavailable"
    assert (await _read(db_session_factory, later.idempotency_key)).status == "pending"


async def test_poll_opens_each_due_thread_once_and_defers_a_failing_opener(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(db_session_factory)
    for thread_id in ("thread-a", "thread-a", "thread-b"):
        await enqueue_wake(
            db_session_factory, _request(tenant_id, thread_id=thread_id), available_at=_NOW
        )
    opened: list[str] = []

    async def _open(thread: WakeThread) -> bool:
        opened.append(thread.thread_id)
        if thread.thread_id == "thread-a":
            raise RuntimeError("platform down")
        return True

    count = await poll_wakes_once(
        db_session_factory, platform="discord", open_thread=_open, now=_NOW
    )
    assert count == 1
    assert sorted(opened) == ["thread-a", "thread-b"]
    due = await list_due_wake_threads(db_session_factory, platform="discord", now=_NOW)
    assert [t.thread_id for t in due] == ["thread-b"], "the failed thread is pushed back"
    later = _NOW + WAKE_UNAVAILABLE_DELAY
    due = await list_due_wake_threads(db_session_factory, platform="discord", now=later)
    assert sorted(t.thread_id for t in due) == ["thread-a", "thread-b"]


async def test_a_thread_with_many_rows_takes_one_slot_in_the_batch(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(db_session_factory)
    for _ in range(150):
        await enqueue_wake(
            db_session_factory, _request(tenant_id, thread_id="busy"), available_at=_NOW
        )
    await enqueue_wake(
        db_session_factory, _request(tenant_id, thread_id="quiet"), available_at=_NOW
    )

    due = await list_due_wake_threads(db_session_factory, platform="discord", now=_NOW)
    assert sorted(t.thread_id for t in due) == ["busy", "quiet"]


async def test_unopenable_threads_do_not_starve_the_101st(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """100 threads the adapter cannot open (no token, archived) ahead of a healthy one.

    Each poll offers at most 100 threads, oldest first. The unavailable ones
    are pushed back, so the very next poll reaches thread 101.
    """
    tenant_id = await _tenant(db_session_factory)
    dead_keys: list[uuid.UUID] = []
    for n in range(100):
        dead = _request(tenant_id, thread_id=f"dead-{n:03d}")
        dead_keys.append(dead.idempotency_key)
        await enqueue_wake(db_session_factory, dead, available_at=_NOW - timedelta(minutes=10))
    healthy = _request(tenant_id, thread_id="healthy")
    await enqueue_wake(db_session_factory, healthy, available_at=_NOW)
    reached: list[str] = []

    async def _open(thread: WakeThread) -> bool:
        if thread.thread_id.startswith("dead-"):
            return False
        reached.append(thread.thread_id)
        return True

    await poll_wakes_once(db_session_factory, platform="discord", open_thread=_open, now=_NOW)
    assert reached == [], "the first batch is the 100 older, unopenable threads"
    await poll_wakes_once(
        db_session_factory, platform="discord", open_thread=_open, now=_NOW + timedelta(seconds=15)
    )
    assert reached == ["healthy"], "the next poll reaches row 101"
    deferred = await _read(db_session_factory, dead_keys[0])
    assert deferred.status == "pending", "unopenable threads are deferred, never dropped"
    assert deferred.available_at == _NOW + WAKE_UNAVAILABLE_DELAY


def _concurrency_dsn() -> str:
    """The real test DSN, for tests that need separate engines (separate processes)."""
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        pytest.skip("DAIMON_DATABASE__TEST_URL must be set for the multi-engine tests")
    return url


async def test_a_wake_survives_a_restart_and_two_pollers_run_it_exactly_once() -> None:
    """Queue in one "process", die, then two fresh "processes" race to run it.

    Each engine is its own connection pool, standing in for a process: the
    first is disposed before anyone polls, so only the committed row carries
    the wake across the restart. Default schema with a fresh tenant, so
    parallel workers cannot collide.
    """
    dsn = _concurrency_dsn()
    before = create_async_engine(dsn)
    try:
        factory = async_sessionmaker(before, expire_on_commit=False)
        tenant_id = await _tenant(factory)
        request = _request(tenant_id, thread_id=f"thread-{uuid.uuid4()}")
        await enqueue_wake(factory, request, available_at=_NOW)
    finally:
        await before.dispose()

    engine_a, engine_b = create_async_engine(dsn), create_async_engine(dsn)
    factory_a = async_sessionmaker(engine_a, expire_on_commit=False)
    factory_b = async_sessionmaker(engine_b, expire_on_commit=False)
    turns: list[str] = []
    try:

        def _opener(factory: async_sessionmaker[AsyncSession], name: str):
            async def _open(thread: WakeThread) -> bool:
                if thread.thread_id != request.thread_id:
                    return True
                claim = await claim_wake(factory, idempotency_key=request.idempotency_key, now=_NOW)
                if claim is None or not await start_wake(factory, claim, now=_NOW):
                    return True
                turns.append(name)
                await settle_wake(factory, claim, status="delivered", now=_NOW)
                return True

            return _open

        async def _poll(factory: async_sessionmaker[AsyncSession], name: str) -> None:
            await poll_wakes_once(
                factory, platform="discord", open_thread=_opener(factory, name), now=_NOW
            )

        await asyncio.gather(_poll(factory_a, "a"), _poll(factory_b, "b"))
        await asyncio.gather(_poll(factory_a, "a"), _poll(factory_b, "b"))

        assert len(turns) == 1, f"exactly one turn may run for one wake, got {turns}"
        row = await _read(factory_a, request.idempotency_key)
        assert row.status == "delivered" and row.attempts == 1
    finally:
        await engine_a.dispose()
        await engine_b.dispose()
