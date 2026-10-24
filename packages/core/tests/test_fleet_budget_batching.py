"""The fleet budget producer's batched reads: pinned statement counts and equivalence.

The producer reads every worker of every live run in the tenant while it holds the
tenant fleet lock. Its accounting reads are batched per graph, not per worker, so the
number of statements one admission runs no longer grows with the workers in the graph.
The equivalence tests compare the batched producer with the unbatched reference (the
per-worker `get_binding` / `read_accounting_revisions` / `read_delegated_usage` reads
and the previous `save_budget`, which re-read every run after writing) on one rich graph.
"""

import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from daimon.core import channel_budget
from daimon.core._models import FleetRun as FleetRunRow
from daimon.core._models import FleetWorker as FleetWorkerRow
from daimon.core.channel_budget import (
    _produce_fleet_budget,
    admit_fleet_worker,
    produce_fleet_budget,
    read_fleet_budget_snapshot,
    require_fleet_worker_admission,
)
from daimon.core.fleet import FleetConflict, worker_slot
from daimon.core.neutral_resources import resolve_worker_spec
from daimon.core.stores import fleet, mux_state
from sqlalchemy import event, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from .test_fleet_budget import (
    POLICY,
    add,
    reserve,
    root,
    setup,
    tariffs,
    usage,
)
from .test_fleet_budget import sm as sm


@dataclass(frozen=True)
class Graph:
    parent: Any  # the caller's run (run 1)
    parents: tuple[Any, ...]  # one WorkerPermissions per run
    runs: tuple[Any, ...]
    bound: Any  # a running, bound worker of run 1 with unbilled usage
    binding: Any


async def _run(sm, parent, run, *, cap="8"):
    async with sm() as db, db.begin():
        other = await fleet.create_run(
            db,
            tenant_id=run.tenant_id,
            orchestrator_account_id=parent.fleet.account_id,
            orchestrator_session_id=run.orchestrator_session_id,
            orchestrator_binding_id=run.orchestrator_binding_id,
            platform=run.platform,
            channel_id=run.channel_id,
            thread_id=run.thread_id,
            operation_key=f"next:{uuid4()}",
            total_budget_usd=Decimal(cap),
            deadline_at=run.deadline_at,
            max_workers=12,
            created_by="host",
        )
    return replace(parent, fleet=replace(parent.fleet, run_id=other.id)), other


async def _claim(sm, run, worker):
    claimed = await fleet.claim_worker(
        sm, tenant_id=run.tenant_id, run_id=run.id, worker_id=worker.id, cap=100
    )
    assert claimed is not None
    return claimed


async def _running(sm, parent, run, ordinal, *, revisions=((1, 1000, 100),), settled=True):
    """A reserved, claimed, bound and running worker with the given usage revisions."""
    worker = await add(sm, parent, ordinal=ordinal)
    assert (await reserve(sm, parent, worker)).admitted
    claimed = await _claim(sm, run, worker)
    for revision, inputs, outputs in revisions:
        await usage(
            sm,
            parent,
            run,
            worker,
            inputs=inputs,
            outputs=outputs,
            revision=revision,
            infrastructure=Decimal(0) if settled else None,
        )
    return await fleet.mark_running(
        sm,
        tenant_id=run.tenant_id,
        worker_id=worker.id,
        expected_version=claimed.version,
        binding_id=f"worker-{worker.id}",
    )


async def uniform_graph(sm, *, runs=3, workers=3) -> Graph:
    """`runs` runs of `workers` running workers each, every one bound with usage."""
    parent, run = await setup(sm, parent=root(budget="10"), funds="100", cap="8", max_workers=12)
    parents, all_runs = [parent], [run]
    for _ in range(runs - 1):
        p, r = await _run(sm, parent, run)
        parents.append(p)
        all_runs.append(r)
    for p, r in zip(parents, all_runs, strict=True):
        for ordinal in range(workers):
            await _running(sm, p, r, ordinal, revisions=((1, 1000, 100), (2, 2000, 200)))
    bound = (await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id))[1][0]
    async with sm() as db:
        binding = await mux_state.get_binding(db, worker_slot(run, bound))
    return Graph(parent, tuple(parents), tuple(all_runs), bound, binding)


async def rich_graph(sm) -> Graph:
    """Three runs with mixed workers: bound and unbound; pending, starting, running and
    terminal; settled debits, a downward `adjust:` correction, and unbilled usage."""
    parent, run1 = await setup(sm, parent=root(budget="10"), funds="100", cap="8", max_workers=12)
    p2, run2 = await _run(sm, parent, run1)
    p3, run3 = await _run(sm, parent, run1)
    # Run 1, the caller's run: its floors are priced from usage.
    # w0: running, settled usage with a downward correction (revision 3 < revision 2).
    await _running(sm, parent, run1, 0, revisions=((1, 1000, 100), (2, 5000, 500), (3, 2000, 200)))
    # w1: running with unbilled usage (no infrastructure, so no ledger debit yet).
    unbilled = await _running(
        sm, parent, run1, 1, revisions=((1, 40_000, 4_000), (2, 90_000, 9_000)), settled=False
    )
    # w2: reserved, claimed, never bound (starting).
    w2 = await add(sm, parent, ordinal=2)
    assert (await reserve(sm, parent, w2)).admitted
    await _claim(sm, run1, w2)
    # w3: reserved, then cancelled before any claim (a never-allocated release, below).
    w3 = await add(sm, parent, ordinal=3)
    assert (await reserve(sm, parent, w3)).admitted
    # w4: reserved, still pending.
    w4 = await add(sm, parent, ordinal=4)
    assert (await reserve(sm, parent, w4)).admitted
    # Run 2: a running bound worker, a failed bound worker with a debit, an unbound pending.
    await _running(sm, p2, run2, 0, revisions=((1, 3000, 300),))
    failed = await _running(sm, p2, run2, 1, revisions=((1, 7000, 700), (2, 4000, 400)))
    await fleet.finish(
        sm,
        tenant_id=run2.tenant_id,
        worker_id=failed.id,
        expected_version=failed.version,
        status="failed",
        error_code="test",
    )
    w = await add(sm, p2, ordinal=2)
    assert (await reserve(sm, p2, w)).admitted
    # Run 3: three running bound workers, one with unbilled usage, one with a correction.
    await _running(sm, p3, run3, 0, revisions=((1, 1000, 100),))
    await _running(sm, p3, run3, 1, revisions=((1, 6000, 600), (2, 3000, 300)))
    await _running(sm, p3, run3, 2, revisions=((1, 20_000, 2_000),), settled=False)
    # Cancelled last, so the producer under test is the one that releases its hold.
    w3 = next(
        w
        for w in (await fleet.run_status(sm, tenant_id=run1.tenant_id, run_id=run1.id))[1]
        if w.ordinal == 3
    )
    await fleet.finish(
        sm,
        tenant_id=run1.tenant_id,
        worker_id=w3.id,
        expected_version=w3.version,
        status="cancelled",
        error_code="test",
    )
    async with sm() as db:
        binding = await mux_state.get_binding(db, worker_slot(run1, unbilled))
    return Graph(parent, (parent, p2, p3), (run1, run2, run3), unbilled, binding)


def _resolved(parent, budget="1"):
    return resolve_worker_spec(
        parent,
        parent.spec,
        policy=POLICY,
        now=datetime.now(UTC),
        limits=parent.limits.model_copy(update={"budget_usd": Decimal(budget)}),
    )


class Statements:
    """Every statement the engine runs while `recording`."""

    def __init__(self, engine) -> None:
        self.engine = engine.sync_engine
        self.recording = False
        self.statements: list[str] = []

    def _before(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if self.recording:
            self.statements.append(statement)

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._before)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(self.engine, "before_cursor_execute", self._before)

    async def count(self, call: Callable[[], Awaitable[Any]]) -> tuple[int, float, Any]:
        self.statements.clear()
        self.recording = True
        started = time.perf_counter()
        try:
            value = await call()
        finally:
            self.recording = False
        return len(self.statements), time.perf_counter() - started, value


def _report(name: str, values: dict[str, Any]) -> None:
    """Optional measurement output for the PR description (DAIMON_PRODUCER_COUNTS=dir)."""
    directory = os.environ.get("DAIMON_PRODUCER_COUNTS")
    if directory:
        with open(os.path.join(directory, f"{name}.json"), "w") as fh:
            json.dump(values, fh, indent=1, default=str)


async def _measure(sm, db_engine, graph: Graph) -> dict[str, tuple[int, float]]:
    parent, run = graph.parent, graph.runs[0]
    prices = tariffs(parent)
    ordinal = (
        max(
            w.ordinal
            for w in (await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id))[1]
        )
        + 1
    )
    resolved = _resolved(parent)
    results: dict[str, tuple[int, float]] = {}
    with Statements(db_engine) as recorder:

        async def admit():
            async with sm() as db, db.begin():
                return await admit_fleet_worker(
                    db,
                    parent=parent,
                    policy=POLICY,
                    resolved=resolved,
                    ordinal=ordinal,
                    role="worker",
                    prices=prices,
                    markup=Decimal(1),
                )

        count, wall, first = await recorder.count(admit)
        assert first.admitted, first.reason
        results["admit_fleet_worker_first"] = (count, wall)
        count, wall, again = await recorder.count(admit)
        assert again.admitted and again.worker.id == first.worker.id
        results["admit_fleet_worker_replay"] = (count, wall)

        async def require():
            async with sm() as db, db.begin():
                return await require_fleet_worker_admission(
                    db,
                    parent=parent,
                    policy=POLICY,
                    worker_id=graph.bound.id,
                    prices=prices,
                    markup=Decimal(1),
                    binding=graph.binding,
                )

        count, wall, required = await recorder.count(require)
        assert required.admitted, required.reason
        results["require_fleet_worker_admission"] = (count, wall)

        async def refresh():
            # The watch's locked refresh: the tenant advisory lock, then the passive producer.
            async with sm() as db, db.begin():
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"fleet:{run.tenant_id}"},
                )
                return await produce_fleet_budget(
                    db, parent=parent, prices=prices, markup=Decimal(1)
                )

        count, wall, _ = await recorder.count(refresh)
        results["watch_locked_refresh"] = (count, wall)
    return results


# Statements per call on 3 runs; the same at 3 and at 6 workers per run.
# Before batching, on 3 runs: 135 / 117 / 103 / 102 statements at 3 workers per run and
# 213 / 195 / 181 / 180 at 6 (about 9 more per worker).
PINNED = {
    "admit_fleet_worker_first": 34,
    "admit_fleet_worker_replay": 23,
    "require_fleet_worker_admission": 18,
    "watch_locked_refresh": 17,
}


@pytest.mark.parametrize("workers", [3, 6])
async def test_producer_statements_do_not_grow_with_the_workers_in_the_graph(
    sm, db_engine, workers
):
    graph = await uniform_graph(sm, runs=3, workers=workers)
    measured = await _measure(sm, db_engine, graph)
    _report(
        f"counts_3x{workers}",
        {k: {"statements": c, "wall_ms": round(w * 1000, 1)} for k, (c, w) in measured.items()},
    )
    assert {name: count for name, (count, _) in measured.items()} == PINNED


# --- Equivalence with the unbatched reference ---------------------------------------


async def _reference_save_budget(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    graph: fleet.BudgetGraph,
    exposures,
    debits,
    zero_spend: frozenset[UUID] = frozenset(),
    settled=None,
) -> fleet.BudgetGraph:
    """`save_budget` before batching: a SUM per run, then every run re-read."""
    for run, workers in graph:
        for worker in workers:
            amount = fleet._amount(exposures[worker.id])
            if (
                amount < worker.reserved_usd
                and worker.id not in zero_spend
                and (settled is None or settled.get(worker.id) != amount)
            ):
                raise FleetConflict("unknown worker exposure cannot be released")
            if amount == worker.reserved_usd:
                continue
            changed = await session.scalar(
                update(FleetWorkerRow)
                .where(
                    FleetWorkerRow.tenant_id == tenant_id,
                    FleetWorkerRow.id == worker.id,
                    FleetWorkerRow.version == worker.version,
                )
                .values(
                    reserved_usd=amount, version=FleetWorkerRow.version + 1, updated_at=func.now()
                )
                .returning(FleetWorkerRow.id)
            )
            if changed is None:
                raise FleetConflict("worker changed during monetary update")
        total = await session.scalar(
            select(func.coalesce(func.sum(FleetWorkerRow.reserved_usd), 0)).where(
                FleetWorkerRow.tenant_id == tenant_id, FleetWorkerRow.run_id == run.id
            )
        )
        observed = fleet._amount(debits[run.id])
        if total == run.reserved_usd and observed == run.observed_usd:
            continue
        changed = await session.scalar(
            update(FleetRunRow)
            .where(
                FleetRunRow.tenant_id == tenant_id,
                FleetRunRow.id == run.id,
                FleetRunRow.version == run.version,
            )
            .values(
                reserved_usd=total,
                observed_usd=observed,
                version=FleetRunRow.version + 1,
                updated_at=func.now(),
            )
            .returning(FleetRunRow.id)
        )
        if changed is None:
            raise FleetConflict("run changed during monetary update")
    return tuple(
        [await fleet.run_status(session, tenant_id=tenant_id, run_id=run.id) for run, _ in graph]
    )


def _unbatched(monkeypatch) -> list[str]:
    """The reference: per-worker reads (no prefetch) and the previous save_budget.

    Returns the binding ids the reference read one by one, to prove it ran.
    """
    reads: list[str] = []
    read_rows = channel_budget.read_accounting_revisions

    async def no_prefetch(*args, **kwargs):
        return None

    async def per_worker(session, *, binding, prefetch=None, **kwargs):
        assert prefetch is None
        reads.append(binding.id)
        return await read_rows(session, binding=binding, **kwargs)

    monkeypatch.setattr(channel_budget, "_prefetch_fleet_money", no_prefetch)
    monkeypatch.setattr(channel_budget, "read_accounting_revisions", per_worker)
    monkeypatch.setattr(fleet, "save_budget", _reference_save_budget)
    return reads


async def _persisted(db: AsyncSession, tenant_id: UUID) -> list[tuple[Any, ...]]:
    runs = (
        await db.execute(
            select(
                FleetRunRow.id,
                FleetRunRow.status,
                FleetRunRow.reserved_usd,
                FleetRunRow.observed_usd,
                FleetRunRow.version,
            )
            .where(FleetRunRow.tenant_id == tenant_id)
            .order_by(FleetRunRow.id)
        )
    ).all()
    workers = (
        await db.execute(
            select(
                FleetWorkerRow.run_id,
                FleetWorkerRow.ordinal,
                FleetWorkerRow.status,
                FleetWorkerRow.binding_id,
                FleetWorkerRow.reserved_usd,
                FleetWorkerRow.version,
            )
            .where(FleetWorkerRow.tenant_id == tenant_id)
            .order_by(FleetWorkerRow.run_id, FleetWorkerRow.ordinal)
        )
    ).all()
    # repr keeps each Decimal's stored scale, so `1` and `1.000000` differ.
    return [repr(tuple(row)) for row in (*runs, *workers)]


def _usages(value: Any) -> str:
    """The decisions with each worker named by (run, ordinal): a new worker's id differs
    between two rolled-back admissions."""
    if isinstance(value, dict):
        return repr([_usages(item) for item in value.values()])
    fields = asdict(value)
    fields["worker"].pop("id")
    return repr(fields)


async def _rolled_back(sm, call: Callable[[AsyncSession], Awaitable[Any]], tenant_id: UUID):
    async with sm() as db:
        transaction = await db.begin()
        try:
            value = await call(db)
            return _usages(value), await _persisted(db, tenant_id)
        finally:
            await transaction.rollback()


@pytest.mark.parametrize(
    "call",
    [
        "admit_new",
        "admit_replay",
        "reserve_producer",
        "require_bound",
        "require_starting",
        "passive",
        "snapshot",
    ],
)
async def test_batched_producer_equals_the_unbatched_reference(sm, monkeypatch, call):
    graph = await rich_graph(sm)
    parent, run = graph.parent, graph.runs[0]
    tenant_id = run.tenant_id
    prices = tariffs(parent)
    _, workers = await fleet.run_status(sm, tenant_id=tenant_id, run_id=run.id)
    starting = next(w for w in workers if w.status == "starting")
    pending = next(w for w in workers if w.status == "pending")

    async def invoke(db: AsyncSession):
        if call in ("admit_new", "admit_replay"):
            target = pending if call == "admit_replay" else None
            return await admit_fleet_worker(
                db,
                parent=parent,
                policy=POLICY,
                resolved=_resolved(parent),
                ordinal=target.ordinal if target else max(w.ordinal for w in workers) + 1,
                role="worker",
                prices=prices,
                markup=Decimal(1),
            )
        if call == "reserve_producer":
            await fleet.lock_budget(
                db,
                tenant_id=tenant_id,
                actor_id=parent.fleet.account_id,
                run_id=run.id,
                persisted=True,
            )
            return await _produce_fleet_budget(
                db,
                parent=parent,
                worker_id=pending.id,
                prices=prices,
                markup=Decimal(1),
                reserve=True,
                financial=True,
            )
        if call in ("require_bound", "require_starting"):
            bound = call == "require_bound"
            return await require_fleet_worker_admission(
                db,
                parent=parent,
                policy=POLICY,
                worker_id=graph.bound.id if bound else starting.id,
                prices=prices,
                markup=Decimal(1),
                binding=graph.binding if bound else None,
            )
        if call == "passive":
            return await produce_fleet_budget(db, parent=parent, prices=prices, markup=Decimal(1))
        raise AssertionError(call)

    if call == "snapshot":
        batched = _usages(
            await read_fleet_budget_snapshot(sm, parent=parent, prices=prices, markup=Decimal(1))
        )
        with monkeypatch.context() as patch:
            reads = _unbatched(patch)
            reference = _usages(
                await read_fleet_budget_snapshot(
                    sm, parent=parent, prices=prices, markup=Decimal(1)
                )
            )
        assert batched == reference and len(reads) >= 7
        return
    batched = await _rolled_back(sm, invoke, tenant_id)
    with monkeypatch.context() as patch:
        reads = _unbatched(patch)
        reference = await _rolled_back(sm, invoke, tenant_id)
    assert batched == reference
    assert len(reads) >= 7  # every bound worker of every run, read one by one


async def test_the_rich_graph_exercises_every_class_of_money(sm):
    graph = await rich_graph(sm)
    run1, run2, run3 = graph.runs
    async with sm() as db:
        ledger = (
            await db.execute(
                text("SELECT idempotency_key, delta_usd FROM tenant_ledger WHERE tenant_id = :t"),
                {"t": run1.tenant_id},
            )
        ).all()
    # A downward correction is a positive `adjust:` row.
    assert any(key.startswith("adjust:") and delta > 0 for key, delta in ledger)
    _, before = await fleet.run_status(sm, tenant_id=run1.tenant_id, run_id=run1.id)
    assert {w.ordinal: w.status for w in before} == {
        0: "running",
        1: "running",
        2: "starting",
        3: "cancelled",
        4: "pending",
    }
    assert next(w for w in before if w.ordinal == 3).reserved_usd == 1
    async with sm() as db, db.begin():
        usages = await produce_fleet_budget(
            db, parent=graph.parent, prices=tariffs(graph.parent), markup=Decimal(1)
        )
    by_ordinal = {usage.worker.ordinal: usage for usage in usages.values()}
    # Unbilled usage on the caller's run: a token floor above its (zero) debit.
    assert by_ordinal[1].worker_floor_usd > 0
    # The never-allocated cancel releases its hold.
    assert by_ordinal[3].worker.reserved_usd == 0
    for run in (run2, run3):
        current, workers = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
        assert current.observed_usd > 0
        assert any(w.binding_id is not None for w in workers)
    _, run2_workers = await fleet.run_status(sm, tenant_id=run2.tenant_id, run_id=run2.id)
    assert {w.status for w in run2_workers} == {"running", "failed", "pending"}


async def _finished_run(sm, parent, run, workers):
    """A terminal run with `workers` terminal, fully settled workers."""
    p, r = await _run(sm, parent, run)
    for ordinal in range(workers):
        worker = await _running(sm, p, r, ordinal, revisions=((1, 1000, 100),))
        await fleet.finish(
            sm,
            tenant_id=r.tenant_id,
            worker_id=worker.id,
            expected_version=worker.version,
            status="failed",
            error_code="test",
        )
    current, _ = await fleet.run_status(sm, tenant_id=r.tenant_id, run_id=r.id)
    await fleet.set_run_status(
        sm,
        tenant_id=r.tenant_id,
        run_id=r.id,
        expected_version=current.version,
        status="failed",
    )
    return p, r


@pytest.mark.parametrize("workers", [2, 8])
async def test_a_finished_runs_workers_do_not_add_statements_per_admission(sm, db_engine, workers):
    """The producer runs under the fleet lock: a finished run must not add reads per worker.

    Before batching, every terminal worker of a terminal run did an active-lease,
    database-clock and send-status read inside the producer's tenant lock (3N statements).
    That convoy made J3/J4/J10 wait behind accumulated finished runs. The bound is the
    same for 2 and for 8 settled workers.
    """
    parent, run = await setup(sm, parent=root(budget="10"), funds="100", cap="8", max_workers=12)
    await _finished_run(sm, parent, run, workers)

    async def call():
        async with sm() as db, db.begin():
            return await produce_fleet_budget(
                db, parent=parent, prices=tariffs(parent), markup=Decimal(1)
            )

    with Statements(db_engine) as recorder:
        count, _, _ = await recorder.count(call)
    assert count < 35, count
