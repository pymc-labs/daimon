"""DB-backed tests for `daimon.core.scheduler.run_one_tick`.

All collaborators are injected (sm, fire). The fire callable is the
success-path writer (calls record_result with the tail); the guarded gather
member is the per-routine error boundary, exercised through run_one_tick.

OQ-3: every record_result write inside the orchestration must run on a fresh
session opened via `sm()`, not the same session as the claim transaction or
the (eventual) fire transaction.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from daimon.core.scheduler import run_one_tick
from daimon.core.stores.domain import FeatureTarget, RoutineRow
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.routines import create_routine, get_routine, record_result
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


async def _seed_due_routine(
    session: AsyncSession,
    *,
    now: datetime,
    trigger: str = "m",
    created_by_user_id: str | None = None,
) -> RoutineRow:
    tenant = await make_tenant(session)
    return await create_routine(
        session,
        tenant_id=tenant.id,
        created_by_user_id=created_by_user_id,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message=trigger,
        next_fire_at=now - timedelta(minutes=1),
    )


async def test_run_one_tick_dispatches_due_routine(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    row = await _seed_due_routine(db_session, now=now)
    await db_session.commit()

    fired: list[uuid.UUID] = []

    async def fake_fire(r: RoutineRow) -> None:
        fired.append(r.id)
        async with db_session_factory() as s, s.begin():
            await record_result(s, r.id, tail="ok", error=None)

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=fake_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )

    assert fired == [row.id], "fire must be called exactly once for the due routine"
    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=row.tenant_id)
    assert fetched is not None
    assert fetched.last_result_tail == "ok", "fire must have written the success tail"


async def test_run_one_tick_records_fire_error_via_gather(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    row = await _seed_due_routine(db_session, now=now)
    await db_session.commit()

    async def boom_fire(_r: RoutineRow) -> None:
        raise ValueError("boom")

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=boom_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=row.tenant_id)
    assert fetched is not None
    assert fetched.last_error is not None
    assert fetched.last_error.startswith("ValueError: boom"), (
        "fire exception must be recorded as last_error with type prefix via the gather path"
    )


async def test_run_one_tick_truncates_long_error_at_500_chars(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    row = await _seed_due_routine(db_session, now=now)
    await db_session.commit()

    async def long_boom_fire(_r: RoutineRow) -> None:
        raise ValueError("x" * 1000)

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=long_boom_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=row.tenant_id)
    assert fetched is not None
    assert fetched.last_error is not None
    assert len(fetched.last_error) <= 500, (
        "last_error must be truncated to 500 chars via the gather path"
    )


async def test_run_one_tick_dispatches_all_concurrently_up_to_cap(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SC-2: with max_concurrent_fires=10 and 10 due routines, all 10 must
    dispatch concurrently. Peak-counter assertion avoids wall-clock flakiness.
    """
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    for _ in range(10):
        # created_by_user_id=None → no cap DB call inside the concurrent window
        await _seed_due_routine(db_session, now=now)
    await db_session.commit()

    concurrent = 0
    peak = 0
    fired: list[uuid.UUID] = []

    async def counting_fire(r: RoutineRow) -> None:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.05)  # DB-free window — do NOT open a session here
        concurrent -= 1
        fired.append(r.id)

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=counting_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )
    assert peak == 10, "all 10 due routines must dispatch concurrently when cap >= 10"
    assert len(fired) == 10, "all 10 routines must fire"


async def test_run_one_tick_bounds_concurrency_to_semaphore(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Semaphore-bound: with max_concurrent_fires=5 and 20 due routines, peak
    concurrency must not exceed 5, and all 20 must eventually fire.
    """
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    for _ in range(20):
        await _seed_due_routine(db_session, now=now)
    await db_session.commit()

    concurrent = 0
    peak = 0
    fired: list[uuid.UUID] = []

    async def counting_fire(r: RoutineRow) -> None:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.05)  # DB-free window
        concurrent -= 1
        fired.append(r.id)

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=counting_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=5,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )
    assert peak == 5, "semaphore must bound peak concurrency to max_concurrent_fires=5"
    assert len(fired) == 20, "all 20 routines must eventually fire despite the semaphore bound"


async def test_run_one_tick_isolates_timed_out_fire_from_sibling(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SC-3: one hung fire (>dispatch_timeout_s) must not block or fail its sibling.
    The hung row records a 'timeout:' error; the sibling's tail is written.
    """
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    hung = await _seed_due_routine(db_session, now=now, trigger="hang")
    sibling = await _seed_due_routine(db_session, now=now, trigger="ok")
    await db_session.commit()

    async def maybe_hang(r: RoutineRow) -> None:
        if r.id == hung.id:
            await asyncio.sleep(10)  # >> dispatch_timeout_s=0.05; wait_for cancels at 0.05s
        else:
            async with db_session_factory() as s, s.begin():
                await record_result(s, r.id, tail="ok", error=None)

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=maybe_hang,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=0.05,
        wait_for_completion=True,
    )

    async with db_session_factory() as s:
        hung_row = await get_routine(s, hung.id, tenant_id=hung.tenant_id)
        sib_row = await get_routine(s, sibling.id, tenant_id=sibling.tenant_id)

    assert hung_row is not None and sib_row is not None
    assert sib_row.last_result_tail == "ok", (
        "sibling fire must complete despite hung sibling timing out"
    )
    assert hung_row.last_error is not None and hung_row.last_error.startswith("timeout:"), (
        "hung fire must record a timeout error starting with 'timeout:'"
    )
    assert hung_row.last_result_tail is None, (
        "timed-out fire must not leave a stray success tail (clean rollback)"
    )


async def test_fire_exception_is_captured_to_sentry_and_still_swallowed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising fire is captured via capture_exception_with_scope
    AND swallowed — gather never raises and last_error is still recorded.
    """
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    row = await _seed_due_routine(db_session, now=now)
    await db_session.commit()

    captured: list[BaseException] = []
    monkeypatch.setattr(
        "daimon.core.scheduler.capture_exception_with_scope",
        lambda exc: captured.append(exc),
    )

    async def boom_fire(_r: RoutineRow) -> None:
        raise ValueError("kaboom")

    # Must not raise (swallow preserved).
    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=boom_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )

    assert len(captured) == 1, "the broad except site must capture exactly the fire's exception"
    assert isinstance(captured[0], ValueError), "captured exception must be the raised ValueError"
    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=row.tenant_id)
    assert fetched is not None and fetched.last_error is not None, (
        "capture must not replace the existing record_result swallow"
    )
    assert fetched.last_error.startswith("ValueError: kaboom"), (
        "last_error must still be recorded alongside the Sentry capture"
    )


async def test_timed_out_fire_is_captured_to_sentry_and_still_swallowed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out fire captures the TimeoutError to Sentry and still
    swallows — the row records the 'timeout:' error.
    """
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    row = await _seed_due_routine(db_session, now=now)
    await db_session.commit()

    captured: list[BaseException] = []
    monkeypatch.setattr(
        "daimon.core.scheduler.capture_exception_with_scope",
        lambda exc: captured.append(exc),
    )

    async def hang_fire(_r: RoutineRow) -> None:
        await asyncio.sleep(10)

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=hang_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=0.05,
        wait_for_completion=True,
    )

    assert len(captured) == 1, "the timeout except site must capture exactly once"
    assert isinstance(captured[0], TimeoutError), (
        "captured exception must be the TimeoutError raised by wait_for"
    )
    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=row.tenant_id)
    assert fetched is not None and fetched.last_error is not None, (
        "timeout capture must not replace the record_result swallow"
    )
    assert fetched.last_error.startswith("timeout:"), "last_error must still record the timeout"


async def test_fire_binds_rid_and_tenant_id_into_log_context(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """While a fire runs, structlog contextvars carry a fresh non-empty rid
    and the row's tenant_id, so every log line _fire_guarded wraps is correlated.
    We snapshot get_contextvars() from inside the fire (capture_logs strips the
    merge_contextvars processor, so the bound ids show up there, not in the line).
    """
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    row = await _seed_due_routine(db_session, now=now)
    await db_session.commit()

    seen: list[dict[str, str]] = []

    async def snapshotting_fire(_r: RoutineRow) -> None:
        seen.append(dict(structlog.contextvars.get_contextvars()))

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=snapshotting_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )

    assert len(seen) == 1, "fire must run exactly once for the due routine"
    bound = seen[0]
    assert bound.get("rid"), "a non-empty rid must be bound while the fire runs"
    assert bound.get("tenant_id") == str(row.tenant_id), (
        "the row's tenant_id must be bound while the fire runs"
    )


async def test_concurrent_fires_get_isolated_rids(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pitfall 2: two fires that interleave at await points must each see
    their OWN rid in contextvars — no cross-contamination. Only true if each fire
    runs in its own copied contextvars context (the create_task fix); bare-coroutine
    gather members share one context and rids leak across.
    """
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    await _seed_due_routine(db_session, now=now, trigger="a")
    await _seed_due_routine(db_session, now=now, trigger="b")
    await db_session.commit()

    # routine_id -> list of rid snapshots taken across the fire's interleaved awaits
    rids_by_routine: dict[str, list[str]] = {}

    async def interleaving_fire(r: RoutineRow) -> None:
        rid_first = structlog.contextvars.get_contextvars().get("rid", "")
        await asyncio.sleep(0)  # yield so the sibling fire runs and (wrongly) could rebind
        rid_second = structlog.contextvars.get_contextvars().get("rid", "")
        rids_by_routine[str(r.id)] = [str(rid_first), str(rid_second)]

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=interleaving_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )

    assert len(rids_by_routine) == 2, "both fires must run"
    for routine_id, rids in rids_by_routine.items():
        assert rids[0] and rids[0] == rids[1], (
            f"routine {routine_id} must see one stable, non-empty rid across its "
            f"interleaved awaits (got {rids}) — drift means contexts were not isolated"
        )
    distinct_rids = {rids[0] for rids in rids_by_routine.values()}
    assert len(distinct_rids) == 2, "the two concurrent fires must carry isolated, distinct rids"


async def test_fire_timeout_records_exact_error_string(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """_fire_guarded timeout branch records exactly 'timeout: exceeded {dispatch_timeout_s}s'.

    The gather still settles (no raise) and the row's last_error equals the
    formatted string — not merely starts with 'timeout:'.
    """
    dispatch_timeout_s = 0.05
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    row = await _seed_due_routine(db_session, now=now)
    await db_session.commit()

    async def hang_fire(_r: RoutineRow) -> None:
        await asyncio.sleep(10)

    # Must complete without raising (gather settles).
    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=hang_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=dispatch_timeout_s,
        wait_for_completion=True,
    )

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=row.tenant_id)

    assert fetched is not None
    expected_error = f"timeout: exceeded {dispatch_timeout_s}s"
    assert fetched.last_error == expected_error, (
        f"timed-out fire must record exactly {expected_error!r}; got {fetched.last_error!r}"
    )


async def test_timed_out_fire_reschedules_to_next_cron_slot(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A3: claim already advances next_fire_at to the next cron slot at claim time.
    The timeout path only records last_error — it must NOT add any next_fire_at logic.
    """
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    row = await _seed_due_routine(
        db_session, now=now
    )  # cron "* * * * *", next_fire_at = now - 1min
    await db_session.commit()

    async def hang_fire(_r: RoutineRow) -> None:
        await asyncio.sleep(10)

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=hang_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=0.05,
        wait_for_completion=True,
    )

    from daimon.core.cron import next_slot_at_or_after

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=row.tenant_id)

    assert fetched is not None
    assert fetched.last_error is not None and fetched.last_error.startswith("timeout:"), (
        "timed-out routine must record a 'timeout:' error"
    )
    expected = next_slot_at_or_after("* * * * *", "UTC", now)
    assert fetched.next_fire_at == expected, (
        "next_fire_at must advance to next cron slot at claim time, "
        "NOT be touched by the timeout path (A3)"
    )


async def test_checks_after_claim_record_failure_completion(
    db_session, db_session_factory, monkeypatch
):
    from unittest.mock import AsyncMock

    import daimon.core.scheduler as scheduler

    now = datetime.now(UTC)
    row = await _seed_due_routine(db_session, now=now, created_by_user_id="owner")
    await db_session.commit()
    monkeypatch.setattr(
        scheduler, "configured_routines", AsyncMock(side_effect=RuntimeError("unavailable"))
    )
    fire = AsyncMock()
    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=1,
        dispatch_timeout_s=30,
        wait_for_completion=True,
    )
    fire.assert_not_awaited()
    async with db_session_factory() as session:
        stored = await get_routine(session, row.id, tenant_id=row.tenant_id)
    assert stored.last_fired_slot == now - timedelta(minutes=1)
    assert stored.last_finished_at is not None
    assert stored.last_finished_claim_at == stored.last_fired_at == now
    assert stored.last_error == "routine_configuration_check_failed"


async def test_run_one_tick_skips_a_routine_its_workspace_turned_off(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    row = await _seed_due_routine(db_session, now=now)
    await set_feature_value(
        db_session,
        tenant_id=row.tenant_id,
        target=FeatureTarget("workspace"),
        key="routines",
        value="off",
        set_by=None,
        surface="chat",
    )
    await db_session.commit()

    async def fire(_r: RoutineRow) -> None:
        raise AssertionError("a routine its workspace turned off must not run")

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=row.tenant_id)
    assert fetched is not None
    assert fetched.last_error == "routines_turned_off", "the owner can see why it did not run"


async def test_run_one_tick_skips_a_routine_whose_feature_cannot_be_read(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed read must not run a routine on the registry default, which is On."""
    from unittest.mock import AsyncMock

    from daimon.core.features import snapshot

    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    row = await _seed_due_routine(db_session, now=now)
    await db_session.commit()
    monkeypatch.setattr(snapshot, "resolve_features", AsyncMock(side_effect=RuntimeError("down")))

    async def fire(_r: RoutineRow) -> None:
        raise AssertionError("a routine whose feature is unreadable must not run")

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=row.tenant_id)
    assert fetched is not None
    assert fetched.last_error == "feature_read_failed", "the owner can see why it did not run"
    assert fetched.next_fire_at == row.next_fire_at, "the slot is given back for the next tick"

    monkeypatch.undo()
    fired: list[RoutineRow] = []

    async def fire_now(r: RoutineRow) -> None:
        fired.append(r)

    await run_one_tick(
        now=now + timedelta(seconds=30),
        sm=db_session_factory,
        fire=fire_now,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )
    assert [r.id for r in fired] == [row.id], "once the feature reads, the lost slot runs"


async def test_run_one_tick_skips_an_intake_routine_when_lead_intake_is_off(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    from unittest.mock import AsyncMock

    import daimon.core.scheduler as scheduler

    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    row = await _seed_due_routine(db_session, now=now)
    await set_feature_value(
        db_session,
        tenant_id=row.tenant_id,
        target=FeatureTarget("workspace"),
        key="lead_intake",
        value="off",
        set_by=None,
        surface="chat",
    )
    await db_session.commit()
    monkeypatch.setattr(
        scheduler, "configured_routines", AsyncMock(return_value={(row.tenant_id, row.id)})
    )
    fire = AsyncMock()
    await run_one_tick(
        now=now,
        sm=db_session_factory,
        fire=fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )
    fire.assert_not_awaited()
    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=row.tenant_id)
    assert fetched is not None
    assert fetched.last_error == "lead_intake_turned_off"
