"""Run-end scheduling retirement through the real fleet and continuation stores."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace

import pytest
from daimon.core._models import Tenant
from daimon.core.continuity.wakes import (
    claim_wake,
    release_wake,
    settle_wake,
    start_wake,
)
from daimon.core.fleet import FleetConflict
from daimon.core.stores import fleet, task_continuations
from daimon.testing.factories import make_account

from .test_worker_finished import _NOW, _arm, _finish_batch, _request, _run, _worker
from .test_worker_finished import pooled as pooled


async def _read(sm, key):
    async with sm() as db:
        return await task_continuations.get_continuation(db, idempotency_key=key)


async def _waits(sm):
    run = await _run(sm, max_workers=7)
    rows, claims, workers = [], [], []
    for index in range(7):
        worker = await _worker(sm, run, index, reviewer_of=workers[0].id if index == 1 else None)
        workers.append(worker)
        async with sm.begin() as db:
            row = await _arm(db, run, [worker])
            if index != 2:
                assert await _finish_batch(db, run, worker)
        if index >= 3:
            claim = await claim_wake(sm, idempotency_key=row.idempotency_key, now=_NOW)
            assert claim is not None
            if index in (4, 5):
                assert await start_wake(sm, claim, now=_NOW)
            if index in (5, 6):
                assert await settle_wake(
                    sm,
                    claim,
                    status="delivered" if index == 5 else "skipped",
                    skip_reason=None if index == 5 else "before_input",
                    now=_NOW,
                )
            claims.append(claim)
        rows.append(await _read(sm, row.idempotency_key))
    return run, rows, claims


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed"])
async def test_run_end_retires_initial_review_unready_and_claimed_waits(pooled, status):
    run, before, claims = await _waits(pooled)
    if status == "completed":
        run = await fleet.set_run_status(
            pooled,
            tenant_id=run.tenant_id,
            run_id=run.id,
            expected_version=run.version,
            status="collecting",
        )
        assert [await _read(pooled, row.idempotency_key) for row in before] == before
    ended = await fleet.set_run_status(
        pooled,
        tenant_id=run.tenant_id,
        run_id=run.id,
        expected_version=run.version,
        status=status,
    )
    for old in before:
        row = await _read(pooled, old.idempotency_key)
        if old.status in {"pending", "claimed"}:
            assert row.status == "cancelled" and row.skip_reason == "run_ended"
            assert row.lease_owner is None and row.lease_expires_at is None
            excluded = {"status", "skip_reason", "lease_owner", "lease_expires_at"}
            assert row.model_dump(exclude=excluded) == old.model_dump(exclude=excluded)
        else:
            assert old.lease_owner is not None
            assert row == old
        assert await claim_wake(pooled, idempotency_key=row.idempotency_key, now=_NOW) is None
    for claim in claims:
        assert not await start_wake(pooled, claim, now=_NOW)
        assert not await release_wake(pooled, claim, retry_at=_NOW)
        assert not await settle_wake(pooled, claim, status="delivered", now=_NOW)
    async with pooled.begin() as db:
        assert await task_continuations.retire_worker_finished_rows(db, run=ended) == 0
    actual, _ = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert actual.status == status and actual.reserved_usd == run.reserved_usd
    assert actual.observed_usd == run.observed_usd


@pytest.mark.parametrize(
    "refusal", ["version", "tenant", "transition", "caller_rollback", "cleanup_error"]
)
async def test_refused_or_rolled_back_run_end_preserves_all_waits(pooled, monkeypatch, refusal):
    run, before, _ = await _waits(pooled)
    if refusal == "cleanup_error":

        async def fail(*args, **kwargs):
            raise RuntimeError("injected cleanup failure")

        monkeypatch.setattr(task_continuations, "retire_worker_finished_rows", fail)
    error = RuntimeError if refusal in {"caller_rollback", "cleanup_error"} else FleetConflict
    with pytest.raises(error):
        async with pooled.begin() as db:
            await fleet.set_run_status(
                db,
                tenant_id=uuid.uuid4() if refusal == "tenant" else run.tenant_id,
                run_id=run.id,
                expected_version=2 if refusal == "version" else run.version,
                status="completed" if refusal == "transition" else "cancelled",
            )
            if refusal == "caller_rollback":
                raise RuntimeError("caller rollback")
    assert [await _read(pooled, row.idempotency_key) for row in before] == before
    actual, _ = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert actual == run


async def test_a_caller_terminal_snapshot_cannot_retire_a_still_running_run(pooled):
    run, before, _ = await _waits(pooled)
    async with pooled.begin() as db:
        assert (
            await task_continuations.retire_worker_finished_rows(
                db, run=replace(run, status="failed")
            )
            == 0
        )
    assert [await _read(pooled, row.idempotency_key) for row in before] == before


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "reason",
        "platform",
        "parent_channel_id",
        "thread_id",
        "requester_account_id",
        "run_id",
    ],
)
async def test_run_end_cannot_retire_an_unrelated_root_reason_or_run(pooled, field):
    run, before, _ = await _waits(pooled)
    request = _request(run).model_copy(
        update={
            "idempotency_key": uuid.uuid4(),
            "requested_work": before[0].requested_work,
        }
    )
    if field == "run_id":
        request = request.model_copy(
            update={
                "requested_work": request.requested_work.replace(str(run.id), str(uuid.uuid4()))
            }
        )
    elif field == "tenant_id":
        request = request.model_copy(update={field: (await _run(pooled)).tenant_id})
    elif field == "requester_account_id":
        async with pooled.begin() as db:
            tenant = await db.get(Tenant, run.tenant_id)
            account = await make_account(db, tenant=tenant)
        request = request.model_copy(update={field: account.id})
    else:
        request = request.model_copy(
            update={
                field: "timer" if field == "reason" else "teams" if field == "platform" else "other"
            }
        )
    # Deliberate unrelated/corrupt reader fixture, never a host publication proof.
    async with pooled.begin() as db:
        await task_continuations.record_continuation(db, **request.model_dump(), available_at=_NOW)
    unrelated = await _read(pooled, request.idempotency_key)
    await fleet.set_run_status(
        pooled,
        tenant_id=run.tenant_id,
        run_id=run.id,
        expected_version=run.version,
        status="cancelled",
    )
    assert await _read(pooled, request.idempotency_key) == unrelated


async def test_corrupt_owned_wait_text_cannot_roll_back_run_end(pooled):
    run, before, _ = await _waits(pooled)
    request = _request(run).model_copy(
        update={
            "idempotency_key": uuid.uuid4(),
            "requested_work": f'{{"run_id":"{run.id}",broken',
        }
    )
    async with pooled.begin() as db:
        await task_continuations.record_continuation(db, **request.model_dump(), available_at=_NOW)
    ended = await fleet.set_run_status(
        pooled,
        tenant_id=run.tenant_id,
        run_id=run.id,
        expected_version=run.version,
        status="failed",
    )
    row = await _read(pooled, request.idempotency_key)
    assert ended.status == "failed" and row.status == "cancelled" and row.skip_reason == "run_ended"
    assert row.requested_work == request.requested_work


@pytest.mark.parametrize("end_first", [False, True])
async def test_publication_and_run_end_race_cannot_leave_or_resurrect_a_wake(pooled, end_first):
    run = await _run(pooled)
    worker = await _worker(pooled, run, 0)
    async with pooled.begin() as db:
        row = await _arm(db, run, [worker])

    async def publish():
        async with pooled.begin() as db:
            return await _finish_batch(db, run, worker)

    async def end():
        return await fleet.set_run_status(
            pooled,
            tenant_id=run.tenant_id,
            run_id=run.id,
            expected_version=run.version,
            status="cancelled",
        )

    await asyncio.gather(*((end(), publish()) if end_first else (publish(), end())))
    retired = await _read(pooled, row.idempotency_key)
    assert retired.status == "cancelled" and retired.lease_owner is None
    assert await claim_wake(pooled, idempotency_key=row.idempotency_key, now=_NOW) is None
    async with pooled.begin() as db:
        from daimon.core.continuity.wakes import enqueue_worker_finished

        assert not await enqueue_worker_finished(
            db, tenant_id=run.tenant_id, run_id=run.id, now=_NOW
        )
