"""J3/J4/J10: a finished fleet run gives back what it did not spend.

SMOKE fc0fa16a: every J3/J4/J10 worker was refused "this server's spending limit was
reached" with $104.69 of credit. Every completed or failed run since 10-16 still held
its whole reservation (observed_usd 0): $102.07 across 74 finished runs, so the next
fleet found ~$0.10 available. A finished run's workers settle to what they spent; the
rest returns to the tenant, so fleets in sequence keep being admitted.
"""

from decimal import Decimal

import pytest
from daimon.core.stores import fleet

from .test_fleet_budget import add, read, reserve, setup
from .test_fleet_budget import sm as sm
from .test_fleet_budget_batching import _claim, _run, _running


async def _finish(sm, parent, run, worker):
    """The worker ends (failed: it spent and stopped) and its run ends with it."""
    await fleet.finish(
        sm,
        tenant_id=run.tenant_id,
        worker_id=worker.id,
        expected_version=worker.version,
        status="failed",
        error_code="test",
    )
    current, _ = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    return await fleet.set_run_status(
        sm,
        tenant_id=run.tenant_id,
        run_id=run.id,
        expected_version=current.version,
        status="failed",
    )


async def test_two_fleets_in_sequence_on_a_small_balance_are_both_admitted(sm):
    # $2.50 of credit, $2 run caps: the second fleet fits only if the first, finished,
    # gives back what it did not spend.
    parent, run = await setup(sm, funds="2.5", cap="2")
    first = await _running(sm, parent, run, 0, revisions=((1, 1000, 100),))
    await _finish(sm, parent, run, first)
    p2, run2 = await _run(sm, parent, run, cap="2")
    second = await add(sm, p2, ordinal=0)
    admitted = await reserve(sm, p2, second)
    assert admitted.admitted, admitted.reason


async def test_a_finished_runs_worker_holds_drop_to_what_it_spent(sm):
    parent, run = await setup(sm, funds="10", cap="2")
    worker = await _running(sm, parent, run, 0, revisions=((1, 1000, 100),))
    failed = await _running(sm, parent, run, 1, revisions=((1, 3000, 300),))
    await fleet.finish(
        sm,
        tenant_id=run.tenant_id,
        worker_id=failed.id,
        expected_version=failed.version,
        status="failed",
        error_code="test",
    )
    _, before = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert {w.reserved_usd for w in before} == {Decimal(1)}  # each holds its whole budget
    current = next(w for w in before if w.id == worker.id)
    await _finish(sm, parent, run, current)
    # Any later producer pass settles the finished run.
    p2, run2 = await _run(sm, parent, run, cap="2")
    await read(sm, p2, await add(sm, p2, ordinal=0))
    ended, workers = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    for w in workers:
        assert Decimal(0) < w.reserved_usd < Decimal("0.1"), (w.status, w.reserved_usd)
    assert ended.reserved_usd == ended.observed_usd == sum(w.reserved_usd for w in workers)


async def test_a_finished_worker_with_unsettled_usage_keeps_its_hold(sm):
    """Usage not yet on the ledger (unknown price or infrastructure) is unknown spend."""
    parent, run = await setup(sm, funds="10", cap="2")
    worker = await _running(sm, parent, run, 0, revisions=((1, 1000, 100),), settled=False)
    await _finish(sm, parent, run, worker)
    p2, run2 = await _run(sm, parent, run, cap="2")
    await read(sm, p2, await add(sm, p2, ordinal=0))
    _, (kept,) = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert kept.reserved_usd == Decimal(1)


async def test_a_settled_worker_of_a_live_run_keeps_its_hold(sm):
    """Only a finished run settles: a live run may still start more spending."""
    parent, run = await setup(sm, funds="10", cap="2")
    worker = await _running(sm, parent, run, 0, revisions=((1, 1000, 100),))
    await fleet.finish(
        sm,
        tenant_id=run.tenant_id,
        worker_id=worker.id,
        expected_version=worker.version,
        status="failed",
        error_code="test",
    )
    await read(sm, parent, await add(sm, parent, ordinal=1))
    _, workers = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert next(w for w in workers if w.id == worker.id).reserved_usd == Decimal(1)


async def test_a_finished_run_with_pending_usage_recovers_once_the_drain_settles_it(sm):
    """Staging's shape: usage recorded while its infrastructure was unknown sat pending,
    so its finished run kept holding. The drain settles it; the next fleet fits."""
    from daimon.core.usage_outbox_drain import drain_pending_usage

    parent, run = await setup(sm, funds="2.5", cap="2")
    worker = await _running(sm, parent, run, 0, revisions=((1, 1000, 100),), settled=False)
    await _finish(sm, parent, run, worker)
    p2, run2 = await _run(sm, parent, run, cap="2")
    second = await add(sm, p2, ordinal=0)
    before = await read(sm, p2, second)
    assert before.prepaid_available_usd < Decimal(2)  # the pending row keeps its hold
    applied, debited = await drain_pending_usage(sm, markup=Decimal(1))
    assert applied == 1 and debited > 0
    # Idempotent: nothing is applied twice.
    assert await drain_pending_usage(sm, markup=Decimal(1)) == (0, Decimal(0))
    admitted = await reserve(sm, p2, second)
    assert admitted.admitted, admitted.reason


async def _claimed_unbound(sm, parent, run, ordinal):
    """Reserved and claimed, then ended before any native binding or input."""
    worker = await add(sm, parent, ordinal=ordinal)
    assert (await reserve(sm, parent, worker)).admitted
    claimed = await _claim(sm, run, worker)
    return await fleet.finish(
        sm,
        tenant_id=run.tenant_id,
        worker_id=worker.id,
        expected_version=claimed.version,
        status="failed",
        error_code="test",
    )


async def test_a_finished_worker_never_bound_or_sent_input_returns_its_whole_hold(sm):
    """No send operation got past pending, so no model call happened: it spent $0."""
    parent, run = await setup(sm, funds="10", cap="2")
    worker = await _claimed_unbound(sm, parent, run, 0)
    assert worker.reserved_usd == Decimal(1) and worker.binding_id is None
    current, _ = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    await fleet.set_run_status(
        sm,
        tenant_id=run.tenant_id,
        run_id=run.id,
        expected_version=current.version,
        status="failed",
    )
    p2, run2 = await _run(sm, parent, run, cap="2")
    await read(sm, p2, await add(sm, p2, ordinal=0))
    ended, (settled,) = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert settled.reserved_usd == 0 and ended.reserved_usd == ended.observed_usd == 0


@pytest.mark.parametrize("proof", ["send_operation", "accepted_input"])
async def test_a_finished_worker_sent_its_input_with_no_usage_recorded_keeps_its_hold(sm, proof):
    """Its input was sent but no usage arrived: spend may exist provider-side. Either
    record proves the send: a processed send operation, or the worker store's count of
    accepted input (some send paths keep no operation row)."""
    from daimon.core._models import FleetWorker as FleetWorkerRow
    from daimon.core._models import MuxOperation
    from daimon.core.fleet import spawn_operation_key
    from sqlalchemy import func, insert, update

    parent, run = await setup(sm, funds="10", cap="2")
    worker = await _claimed_unbound(sm, parent, run, 0)
    async with sm() as db, db.begin():
        if proof == "accepted_input":
            await db.execute(
                update(FleetWorkerRow).where(FleetWorkerRow.id == worker.id).values(turns_used=1)
            )
        else:
            await db.execute(
                insert(MuxOperation).values(
                    tenant_id=run.tenant_id,
                    account_id=str(run.orchestrator_account_id),
                    key=spawn_operation_key(run.id, worker.ordinal) + ":send:0",
                    principal_id=str(run.orchestrator_account_id),
                    status="processed",
                    record={},
                    updated_at=func.now(),
                )
            )
    current, _ = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    await fleet.set_run_status(
        sm,
        tenant_id=run.tenant_id,
        run_id=run.id,
        expected_version=current.version,
        status="failed",
    )
    p2, run2 = await _run(sm, parent, run, cap="2")
    await read(sm, p2, await add(sm, p2, ordinal=0))
    _, (kept,) = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert kept.reserved_usd == Decimal(1)


@pytest.mark.parametrize("evidence", ["lease", "unanswered_send"])
async def test_a_deadline_swept_worker_still_mid_turn_keeps_its_hold(sm, evidence):
    """The deadline sweep marks a worker failed without stopping its native turn: an
    unexpired lease on its slot, or an unanswered send, means it may still be spending,
    even with its earlier usage all on the ledger."""
    from datetime import timedelta

    from daimon.core._models import MuxOperation
    from daimon.core.fleet import spawn_operation_key, worker_slot
    from daimon.core.stores import mux_state
    from sqlalchemy import func, insert

    parent, run = await setup(sm, funds="10", cap="2")
    worker = await _running(sm, parent, run, 0, revisions=((1, 1000, 100),))
    async with sm() as db, db.begin():
        if evidence == "lease":
            await mux_state.acquire_lease(
                db,
                worker_slot(run, worker),
                holder="worker",
                turn_id="current",
                clock=mux_state.database_clock,
                ttl=timedelta(minutes=5),
            )
        else:
            await db.execute(
                insert(MuxOperation).values(
                    tenant_id=run.tenant_id,
                    account_id=str(run.orchestrator_account_id),
                    key=spawn_operation_key(run.id, worker.ordinal) + ":send:0",
                    principal_id=str(run.orchestrator_account_id),
                    status="sent",
                    record={},
                    updated_at=func.now(),
                )
            )
    await _finish(sm, parent, run, worker)
    p2, run2 = await _run(sm, parent, run, cap="2")
    await read(sm, p2, await add(sm, p2, ordinal=0))
    _, (kept,) = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert kept.reserved_usd == Decimal(1)
