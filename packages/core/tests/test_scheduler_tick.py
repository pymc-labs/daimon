"""DB-backed tests for `daimon.core.scheduler.run_one_tick`.

All collaborators are injected (sm, caps, fire). The fire callable is the
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
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import create_routine, get_routine, record_result
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class _FakeCaps:
    def __init__(self, *, over: bool = False, raises: bool = False) -> None:
        self.over = over
        self.raises = raises
        self.calls: list[tuple[uuid.UUID, str | None]] = []

    async def is_over_cap(self, tenant_id: uuid.UUID, user_id: str | None) -> bool:
        self.calls.append((tenant_id, user_id))
        if self.raises:
            raise RuntimeError("caps boom")
        return self.over


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

    caps = _FakeCaps(over=False)

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        caps=caps,
        fire=fake_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
    )

    assert fired == [row.id], "fire must be called exactly once for the due routine"
    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id)
    assert fetched is not None
    assert fetched.last_result_tail == "ok", "fire must have written the success tail"


async def test_run_one_tick_records_cap_error(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    row = await _seed_due_routine(db_session, now=now, created_by_user_id="u1")
    await db_session.commit()

    fired: list[uuid.UUID] = []

    async def fake_fire(r: RoutineRow) -> None:
        fired.append(r.id)

    caps = _FakeCaps(over=True)

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        caps=caps,
        fire=fake_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
    )

    assert fired == [], "over-cap routine must not be fired"
    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id)
    assert fetched is not None
    assert fetched.last_error == "cap_exceeded", "cap-blocked routine must record the cap error"


async def test_run_one_tick_swallows_cap_block_record_result_error(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for BL-03: a transient DB error inside the cap-block
    record_result must be logged and swallowed, not propagate out and kill
    the tick. This keeps the cap-block path symmetric with the gather member's
    own error boundary.
    """
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    await _seed_due_routine(db_session, now=now, created_by_user_id="u1")
    await db_session.commit()

    fired: list[uuid.UUID] = []

    async def fake_fire(r: RoutineRow) -> None:
        fired.append(r.id)

    async def boom_record_result(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("transient db error")

    monkeypatch.setattr(
        "daimon.core.stores.routines.record_result",
        boom_record_result,
    )

    caps = _FakeCaps(over=True)

    # Must not raise.
    await run_one_tick(
        now=now,
        sm=db_session_factory,
        caps=caps,
        fire=fake_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
    )
    assert fired == [], "over-cap routine must not be fired"


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
        caps=_FakeCaps(),
        fire=boom_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
    )

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id)
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
        caps=_FakeCaps(),
        fire=long_boom_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
    )

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id)
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
        caps=_FakeCaps(),
        fire=counting_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
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
        caps=_FakeCaps(),
        fire=counting_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=5,
        dispatch_timeout_s=5.0,
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
        caps=_FakeCaps(),
        fire=maybe_hang,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=0.05,
    )

    async with db_session_factory() as s:
        hung_row = await get_routine(s, hung.id)
        sib_row = await get_routine(s, sibling.id)

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
        caps=_FakeCaps(),
        fire=boom_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
    )

    assert len(captured) == 1, "the broad except site must capture exactly the fire's exception"
    assert isinstance(captured[0], ValueError), "captured exception must be the raised ValueError"
    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id)
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
        caps=_FakeCaps(),
        fire=hang_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=0.05,
    )

    assert len(captured) == 1, "the timeout except site must capture exactly once"
    assert isinstance(captured[0], TimeoutError), (
        "captured exception must be the TimeoutError raised by wait_for"
    )
    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id)
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
        caps=_FakeCaps(),
        fire=snapshotting_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
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
        caps=_FakeCaps(),
        fire=interleaving_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
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
        caps=_FakeCaps(),
        fire=hang_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=dispatch_timeout_s,
    )

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id)

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
        caps=_FakeCaps(),
        fire=hang_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=0.05,
    )

    from daimon.core.cron import next_slot_at_or_after

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id)

    assert fetched is not None
    assert fetched.last_error is not None and fetched.last_error.startswith("timeout:"), (
        "timed-out routine must record a 'timeout:' error"
    )
    expected = next_slot_at_or_after("* * * * *", "UTC", now)
    assert fetched.next_fire_at == expected, (
        "next_fire_at must advance to next cron slot at claim time, "
        "NOT be touched by the timeout path (A3)"
    )
