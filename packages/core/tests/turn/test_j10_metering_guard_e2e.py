"""Offline guard for smoke-5 OWNER 3: the budget watch's metering kill and the timeouts.

Smoke-5 (26250cd6, nc-fleet's verdict): under a spawn burst the watch FAILED CLOSED on
usage that was still readable, and a cancelled worker that was still waiting for a
slot was never recorded:

- the lock-free snapshot read (the whole producer graph) took longer than the watch's
  2 s read timeout three polls in a row, so a run's workers were cancelled
  ``usage_unavailable`` mid-run;
- one idle-in-transaction holder kept the tenant fleet lock for 62 s, so another run's
  worker was cancelled ``usage_unavailable`` although its snapshot reads succeeded;
- the cancel ended the claim loop of still-pending workers without recording them, so
  they sat ``pending`` until the deadline sweep ended them ``deadline_expired``.

Each test reproduces one of these with injected latency. On the code that ran smoke-5
they FAIL; the fix makes them pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from daimon.core import channel_budget
from daimon.core.fleet_orchestration import FleetOrchestrator, HostFleetPorts
from daimon.core.stores import fleet
from sqlalchemy.exc import DBAPIError

from .test_fleet_native_dispatch import _policy, _spawn_terms
from .test_fleet_native_dispatch import worker_root as worker_root
from .test_frozen_root import root_turn as root_turn
from .test_openai_worker_plan import application as application
from .test_openai_worker_plan import claimed_openai_worker as claimed_openai_worker
from .test_openai_worker_plan import db_preflight as db_preflight
from .test_openai_worker_plan import db_session_factory as db_session_factory
from .test_openai_worker_plan import owned as owned
from .test_openai_worker_plan import owned_database_preflight as owned_database_preflight
from .test_openai_worker_plan import provider as provider

#: Smoke-5's measured slow producer read, and its idle lock holder.
SLOW_READ_S = 2.5
LOCK_HELD_S = 62.0


# -- The watch itself, with its reads injected ---------------------------------------


class _LockTimeout(Exception):
    sqlstate = "55P03"

    def __str__(self) -> str:
        return "canceling statement due to lock timeout"


class _Session:
    def __init__(self, lock_busy: bool) -> None:
        self._lock_busy = lock_busy

    async def execute(self, statement: object, params: object = None) -> None:
        if self._lock_busy and "pg_advisory_xact_lock" in str(statement):
            # The watch's queued lock attempt ends in Postgres's lock timeout.
            raise DBAPIError(str(statement), params, _LockTimeout())

    def begin_nested(self) -> contextlib.AbstractAsyncContextManager[None]:
        return contextlib.nullcontext()  # type: ignore[return-value]


class _Sessions:
    """A sessionmaker whose tenant fleet lock is held by someone else, or free."""

    def __init__(self, lock_busy: bool) -> None:
        self.lock_busy = lock_busy

    @contextlib.asynccontextmanager
    async def begin(self):
        yield _Session(self.lock_busy)


def _watched(n: int = 3) -> tuple[SimpleNamespace, dict[uuid.UUID, asyncio.Event]]:
    tenant = uuid.uuid4()
    parent = SimpleNamespace(
        fleet=SimpleNamespace(
            scope=SimpleNamespace(tenant_id=str(tenant)),
            run_id=uuid.uuid4(),
            enabled=True,
            depth=0,
        )
    )
    return parent, {uuid.uuid4(): channel_budget.WorkerCancel() for _ in range(n)}


def _readable(cancelled):
    """Every watched worker is admitted: its usage is readable and within budget."""
    return {worker_id: SimpleNamespace(admitted=True, reason=None) for worker_id in cancelled}


async def _watch_for(seconds: float, *, sessions, parent, cancelled) -> str:
    finished = asyncio.Event()
    task = asyncio.create_task(
        channel_budget.watch_fleet_budget(
            sessionmaker=sessions,  # type: ignore[arg-type]
            parent=parent,  # type: ignore[arg-type]
            prices={},
            markup=Decimal(1),
            cancelled=cancelled,
            finished=finished,
        )
    )
    try:
        async with asyncio.timeout(seconds):
            return await asyncio.shield(task)
    except TimeoutError:
        finished.set()
        return await task


async def test_slow_but_successful_reads_never_cancel_a_worker(monkeypatch):
    """Smoke-5 J3: the spawn burst's admissions kept the tenant lock busy (a new short
    holder each time), so the watch decided from lock-free snapshot reads; three of
    them in a row took 2.5 s (over the 2 s read timeout), each one SUCCEEDING, and the
    run's workers were cancelled ``usage_unavailable`` mid-run."""
    parent, cancelled = _watched()
    holders = iter(range(10_000, 20_000))

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(SLOW_READ_S)
        return _readable(cancelled)

    async def holder(_session, _key):
        return {"pid": next(holders), "xact_start": "admission"}

    monkeypatch.setattr(channel_budget, "read_fleet_budget_snapshot", slow)
    monkeypatch.setattr(channel_budget, "_lock_holder", holder)
    outcome = await _watch_for(
        15, sessions=_Sessions(lock_busy=True), parent=parent, cancelled=cancelled
    )
    reasons = [event.reason for event in cancelled.values() if event.is_set()]
    assert outcome == "finished" and reasons == [], (
        f"the watch failed closed on slow but readable usage: {outcome}, {reasons}"
    )


async def test_a_long_lock_holder_never_cancels_while_snapshots_succeed(monkeypatch):
    """Smoke-5 J10: one idle-in-transaction holder kept the tenant fleet lock 62.6 s.

    Every lock-free snapshot read in that time succeeded, yet the run's worker was
    cancelled ``usage_unavailable``. Usage read successfully moments ago is readable.
    """
    parent, cancelled = _watched(1)

    async def snapshot(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return _readable(cancelled)

    async def holder(_session, _key):
        return {"pid": 452042, "xact_start": "idle-in-transaction"}

    monkeypatch.setattr(channel_budget, "read_fleet_budget_snapshot", snapshot)
    monkeypatch.setattr(channel_budget, "_lock_holder", holder)
    outcome = await _watch_for(
        LOCK_HELD_S + 3, sessions=_Sessions(lock_busy=True), parent=parent, cancelled=cancelled
    )
    reasons = [event.reason for event in cancelled.values() if event.is_set()]
    assert outcome == "finished" and reasons == [], (
        f"a held lock cancelled a worker whose usage stayed readable: {outcome}, {reasons}"
    )


# -- Through the real orchestrator ----------------------------------------------------


def _ports(request, prices, *, max_per_tenant: int) -> HostFleetPorts:
    async def preparation(parent):
        return request

    return HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        preparation=preparation,
        pool=fleet.WorkerPool(
            max_concurrent_workers=max_per_tenant, max_concurrent_workers_per_tenant=max_per_tenant
        ),
        deps=replace(request.deps, fleet_enabled=True),
    )


async def _row(request, root, worker_id: str):
    _, rows = await fleet.run_status(
        request.deps.sessionmaker, tenant_id=request.tenant_id, run_id=root.fleet.run_id
    )
    return next(row for row in rows if str(row.id) == worker_id)


async def test_a_cancel_records_a_still_pending_worker_at_once(worker_root):
    """Smoke-5 J10 opus/astra: the usage_unavailable cancel ended their claim loop with
    nothing recorded; they sat ``pending`` until the sweep ended them ``deadline_expired``
    nine minutes later, holding the run open. The cancel must be recorded at once."""
    request, root, _, resolved, prices, _, _, _, _ = worker_root
    # The fixture's seeded worker holds the tenant's only slot, so this one waits in its
    # claim loop, as smoke-5's did behind the tenant cap.
    ports = _ports(request, prices, max_per_tenant=1)
    owner = FleetOrchestrator(ports)
    try:
        waiting = await owner.spawn(
            root, await _policy(request), **_spawn_terms(request, root, resolved, "waits")
        )
        await asyncio.sleep(0.5)
        assert (await _row(request, root, waiting["worker_id"])).status == "pending"
        events = ports._watches[root.fleet.run_id][0]  # pyright: ignore[reportPrivateUsage]
        channel_budget.cancel_workers(events, "usage_unavailable", run_id=root.fleet.run_id)
        started = time.monotonic()
        row = await _row(request, root, waiting["worker_id"])
        while row.status == "pending" and time.monotonic() - started < 5:
            await asyncio.sleep(0.05)
            row = await _row(request, root, waiting["worker_id"])
        assert row.status != "pending", "a cancelled pending worker was left for the deadline sweep"
        assert row.error_code == "usage_unavailable", row.error_code
    finally:
        await ports.close()


@pytest.mark.parametrize("stop", ["worker", "fleet"])
async def test_a_stopped_pending_worker_ends_as_stopped_never_as_failed(worker_root, stop):
    """#1462 review: a person's Stop on a worker still waiting for its slot (or Stop-all on
    its fleet) reaches the claim loop as the watch's cancel ("budget" for a stopped
    worker, "terminal" for a stopped fleet). It must end the worker cancelled /
    stop_requested, as commit_worker_transition does, never failed with the cancel's
    failure code."""
    request, root, _, resolved, prices, _, _, _, _ = worker_root
    ports = _ports(request, prices, max_per_tenant=1)
    owner = FleetOrchestrator(ports)
    try:
        waiting = await owner.spawn(
            root, await _policy(request), **_spawn_terms(request, root, resolved, "stops")
        )
        await asyncio.sleep(0.5)
        row = await _row(request, root, waiting["worker_id"])
        assert row.status == "pending"
        if stop == "worker":
            await fleet.request_worker_stop(
                request.deps.sessionmaker,
                tenant_id=request.tenant_id,
                worker_id=row.id,
                expected_version=row.version,
            )
            reason = "budget"
        else:
            run, _ = await fleet.run_status(
                request.deps.sessionmaker, tenant_id=request.tenant_id, run_id=root.fleet.run_id
            )
            await fleet.set_run_status(
                request.deps.sessionmaker,
                tenant_id=request.tenant_id,
                run_id=run.id,
                expected_version=run.version,
                status="cancelled",
            )
            reason = "terminal"
        events = ports._watches[root.fleet.run_id][0]  # pyright: ignore[reportPrivateUsage]
        channel_budget.cancel_workers(events, reason, run_id=root.fleet.run_id)
        started = time.monotonic()
        row = await _row(request, root, waiting["worker_id"])
        while row.status == "pending" and time.monotonic() - started < 5:
            await asyncio.sleep(0.05)
            row = await _row(request, root, waiting["worker_id"])
        assert (row.status, row.error_code) == ("cancelled", "stop_requested")
    finally:
        await ports.close()


async def test_the_watch_still_fails_closed_once_reads_fail_for_the_whole_window(monkeypatch):
    """The window delays failing closed; it never removes it. With a 0.5 s window and
    every read slower than its timeout, the watch stops the run's workers soon after."""
    parent, cancelled = _watched()
    holders = iter(range(30_000, 40_000))

    async def never(*_args, **_kwargs):
        await asyncio.sleep(60)

    async def holder(_session, _key):
        return {"pid": next(holders), "xact_start": "admission"}

    monkeypatch.setattr(channel_budget, "BUDGET_READ_FAILURE_WINDOW_S", 0.5)
    monkeypatch.setattr(channel_budget, "read_fleet_budget_snapshot", never)
    monkeypatch.setattr(channel_budget, "_lock_holder", holder)
    finished = asyncio.Event()
    started = time.monotonic()
    outcome = await asyncio.wait_for(
        channel_budget.watch_fleet_budget(
            sessionmaker=_Sessions(lock_busy=True),  # type: ignore[arg-type]
            parent=parent,  # type: ignore[arg-type]
            prices={},
            markup=Decimal(1),
            cancelled=cancelled,
            finished=finished,
            poll_interval=0.05,
            read_timeout=0.1,
        ),
        timeout=10,
    )
    assert outcome == "usage_unavailable"
    assert {event.reason for event in cancelled.values()} == {"usage_unavailable"}
    assert time.monotonic() - started < 5


async def test_the_watch_s_first_cause_survives_a_deferred_failure_and_the_sweep(
    worker_root, monkeypatch
):
    """J10b (run 4fc62626, smoke dce44708): the watch cancelled every worker for
    `usage_unavailable`, the workers' own failure writes hit DBAPIError under the same
    contention and were deferred, and the deadline sweep ended them
    `deadline_expired`: the real cause was lost. The first decision is recorded on
    its own, so the sweep keeps it."""
    from sqlalchemy import text

    request, root, _, resolved, prices, _, _, _, _ = worker_root
    real_finish = fleet.finish

    async def busy_finish(*args, **kwargs):
        raise DBAPIError("UPDATE fleet_worker", None, RuntimeError("lock busy"))

    async def blind_watch(*, cancelled, finished, **_kwargs):
        while not cancelled:  # the waiting worker registers its cancel event
            await asyncio.sleep(0.02)
        channel_budget.cancel_workers(cancelled, "usage_unavailable", run_id=root.fleet.run_id)
        return "usage_unavailable"

    monkeypatch.setattr(channel_budget, "watch_fleet_budget", blind_watch)
    monkeypatch.setattr(fleet, "finish", busy_finish)
    ports = _ports(request, prices, max_per_tenant=1)
    owner = FleetOrchestrator(ports)
    try:
        waiting = await owner.spawn(
            root, await _policy(request), **_spawn_terms(request, root, resolved, "waits")
        )
        started = time.monotonic()
        row = await _row(request, root, waiting["worker_id"])
        while row.error_code is None and time.monotonic() - started < 5:
            await asyncio.sleep(0.05)
            row = await _row(request, root, waiting["worker_id"])
        assert row.status == "pending", "its own failure write was deferred, as in J10b"
    finally:
        await ports.close()
    monkeypatch.setattr(fleet, "finish", real_finish)
    async with request.deps.sessionmaker.begin() as db:
        await db.execute(
            text("UPDATE fleet_run SET deadline_at=now()-interval '1 second' WHERE id=:id"),
            {"id": root.fleet.run_id},
        )
    await fleet.sweep_expired(request.deps.sessionmaker, tenant_id=request.tenant_id)
    row = await _row(request, root, waiting["worker_id"])
    assert (row.status, row.error_code) == ("failed", "usage_unavailable"), row.error_code
