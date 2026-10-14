"""Run-scoped audit reads retain queue facts without authorizing a turn."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from daimon.core._models import FleetRun, TaskContinuation, Tenant
from daimon.core.continuity.wakes import cancel_wake
from daimon.core.stores import UnflushedReadState, fleet, task_continuations
from sqlalchemy import event, insert, select, text

from .test_run_end import _read, _waits
from .test_worker_finished import pooled as pooled


async def test_run_read_preserves_all_statuses_and_audit_in_read_only_transaction(pooled):
    run, initial, _ = await _waits(pooled)
    assert await cancel_wake(
        pooled, tenant_id=run.tenant_id, idempotency_key=initial[2].idempotency_key
    )
    expected = tuple(
        sorted(
            [await _read(pooled, row.idempotency_key) for row in initial],
            key=lambda row: (row.created_at, row.id),
        )
    )
    assert {row.status for row in expected} == {
        "pending",
        "claimed",
        "delivered",
        "skipped",
        "cancelled",
    }
    assert all(
        row.lease_owner is not None for row in expected if row.status in {"delivered", "skipped"}
    )
    async with pooled.begin() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        for _ in range(2):
            assert (
                await task_continuations.list_worker_finished_for_run(
                    db,
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                )
                == expected
            )
    assert [await _read(pooled, row.idempotency_key) for row in expected] == list(expected)
    actual, _ = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert actual == run


async def test_scope_precedes_decode_and_requires_an_actual_owned_run(pooled, monkeypatch):
    run, owned, _ = await _waits(pooled)
    foreign, _, _ = await _waits(pooled)
    absent = uuid.uuid4()
    clone = owned[0].model_dump(exclude={"id"})
    async with pooled.begin() as db:
        # Reader-only adversarial fixtures, never valid frozen batch/authority proofs.
        for changes in (
            {
                "tenant_id": foreign.tenant_id,
                "requester_account_id": foreign.orchestrator_account_id,
            },
            {"requested_work": f'{{"run_id":"{absent}",broken'},
            {"reason": "timer"},
            {"requested_work": clone["requested_work"].replace('"run_id"', '"runXid"', 1)},
        ):
            await db.execute(
                insert(TaskContinuation).values(
                    **(clone | changes | {"idempotency_key": uuid.uuid4()}),
                )
            )
    expected = tuple(sorted(owned, key=lambda row: (row.created_at, row.id)))
    original = task_continuations.TaskContinuationRow.model_validate
    decoded = []

    def guarded(row, *args, **kwargs):
        assert row["id"] in {item.id for item in owned}
        decoded.append(row["id"])
        return original(row, *args, **kwargs)

    monkeypatch.setattr(task_continuations.TaskContinuationRow, "model_validate", guarded)
    async with pooled() as db:
        assert (
            await task_continuations.list_worker_finished_for_run(
                db, tenant_id=run.tenant_id, run_id=run.id
            )
            == expected
        )
        assert (
            await task_continuations.list_worker_finished_for_run(
                db, tenant_id=foreign.tenant_id, run_id=run.id
            )
            == ()
        )
        assert (
            await task_continuations.list_worker_finished_for_run(
                db, tenant_id=run.tenant_id, run_id=foreign.id
            )
            == ()
        )
        assert (
            await task_continuations.list_worker_finished_for_run(
                db, tenant_id=run.tenant_id, run_id=absent
            )
            == ()
        )
    assert decoded == [row.id for row in expected]


async def test_owned_malformed_body_and_bad_root_stay_visible_for_consumer_refusal(pooled):
    run, owned, _ = await _waits(pooled)
    clone = owned[0].model_dump(exclude={"id"})
    keys = [uuid.uuid4(), uuid.uuid4()]
    body = f'{{"run_id":"{run.id}",broken'
    async with pooled.begin() as db:
        for key, changes in zip(
            keys,
            (
                {"requested_work": body, "created_at": owned[0].created_at - timedelta(days=1)},
                {"thread_id": "other-root"},
            ),
            strict=True,
        ):
            await db.execute(
                insert(TaskContinuation).values(**(clone | changes | {"idempotency_key": key}))
            )
    async with pooled() as db:
        rows = await task_continuations.list_worker_finished_for_run(
            db, tenant_id=run.tenant_id, run_id=run.id
        )
    assert len(rows) == len(owned) + 2
    assert rows[0].idempotency_key == keys[0]
    assert next(row for row in rows if row.idempotency_key == keys[0]).requested_work == body
    assert next(row for row in rows if row.idempotency_key == keys[1]).thread_id == "other-root"


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize("model", [TaskContinuation, FleetRun])
@pytest.mark.parametrize("mutation", ["dirty", "deleted"])
async def test_persistent_wake_or_run_edits_refuse_before_query_without_flush(
    pooled, read_only, autoflush, model, mutation
):
    run, initial, _ = await _waits(pooled)
    before = await _read(pooled, initial[0].idempotency_key)
    async with pooled() as db:
        if read_only:
            await db.execute(text("SET TRANSACTION READ ONLY"))
        orm = await db.get(model, before.id if model is TaskContinuation else run.id)
        db.autoflush = autoflush
        if mutation == "deleted":
            await db.delete(orm)
        else:
            orm.status = "cancelled"
            if model is TaskContinuation:
                orm.requested_work = "{}"
        dirty, deleted = set(db.dirty), set(db.deleted)
        queries, flushes = [], []
        event.listen(db.sync_session, "do_orm_execute", queries.append)
        event.listen(db.sync_session, "before_flush", lambda *args: flushes.append(args))
        op = "delete" if mutation == "deleted" else "update"
        with pytest.raises(
            UnflushedReadState,
            match=f"^unflushed {op} on {model.__tablename__} before a persisted read$",
        ):
            await task_continuations.list_worker_finished_for_run(
                db, tenant_id=run.tenant_id, run_id=run.id
            )
        assert queries == flushes == []
        assert set(db.dirty) == dirty and set(db.deleted) == deleted
        if mutation == "dirty":
            assert orm.status == "cancelled"
            if model is TaskContinuation:
                assert orm.requested_work == "{}"
    assert await _read(pooled, before.idempotency_key) == before
    actual, _ = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert actual == run


@pytest.mark.parametrize("read_only", [False, True])
async def test_wake_core_update_bypasses_clean_cache_without_overwriting_it(pooled, read_only):
    run, initial, _ = await _waits(pooled)
    before = await _read(pooled, initial[0].idempotency_key)
    async with pooled() as db:
        if read_only:
            await db.execute(text("SET TRANSACTION READ ONLY"))
        cached = await db.get(TaskContinuation, before.id)
        statement = (
            TaskContinuation.__table__.update()
            .where(TaskContinuation.id == before.id)
            .values(status="cancelled")
        )
        if read_only:
            # READ COMMITTED sees a real committed writer; this reader cannot write.
            async with pooled.begin() as writer:
                await writer.execute(statement)
        else:
            await db.execute(statement)
        assert cached.status == before.status and not db.dirty
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *args: flushes.append(args))
        persisted = (
            (
                await db.execute(
                    select(*TaskContinuation.__table__.c).where(TaskContinuation.id == before.id)
                )
            )
            .mappings()
            .one()
        )
        expected = task_continuations.TaskContinuationRow.model_validate(dict(persisted))
        for _ in range(2):
            rows = await task_continuations.list_worker_finished_for_run(
                db, tenant_id=run.tenant_id, run_id=run.id
            )
            assert next(row for row in rows if row.id == before.id) == expected
        after = (
            (
                await db.execute(
                    select(*TaskContinuation.__table__.c).where(TaskContinuation.id == before.id)
                )
            )
            .mappings()
            .one()
        )
        assert dict(after) == dict(persisted)
        assert expected.status == "cancelled" and flushes == []
        assert cached.status == before.status and not db.dirty
    assert await _read(pooled, before.idempotency_key) == (expected if read_only else before)


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize("unrelated", ["dirty", "deleted"])
async def test_wake_read_keeps_new_unrelated_and_reverted_caller_state_unflushed(
    pooled, read_only, autoflush, unrelated
):
    run, initial, _ = await _waits(pooled)
    expected = tuple(sorted(initial, key=lambda row: (row.created_at, row.id)))
    async with pooled() as db:
        if read_only:
            await db.execute(text("SET TRANSACTION READ ONLY"))
        tenant = await db.get(Tenant, run.tenant_id)
        external_id = tenant.external_id
        cached = await db.get(TaskContinuation, initial[0].id)
        cached.status = "cancelled"
        cached.status = initial[0].status  # A reverted assignment is not a persisted edit.
        if unrelated == "dirty":
            tenant.external_id = "caller-unflushed"
        else:
            await db.delete(tenant)
        pending = TaskContinuation(
            **(initial[0].model_dump(exclude={"id"}) | {"idempotency_key": uuid.uuid4()})
        )
        db.add(pending)
        db.autoflush = autoflush
        dirty, new, deleted = set(db.dirty), set(db.new), set(db.deleted)
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *args: flushes.append(args))
        assert (
            await task_continuations.list_worker_finished_for_run(
                db, tenant_id=run.tenant_id, run_id=run.id
            )
            == expected
        )
        assert flushes == [] and set(db.new) == new and set(db.dirty) == dirty
        assert set(db.deleted) == deleted and pending.id is None
        assert tenant.external_id == ("caller-unflushed" if unrelated == "dirty" else external_id)
        assert cached.status == initial[0].status
    assert [await _read(pooled, row.idempotency_key) for row in initial] == initial
    async with pooled() as db:
        assert (await db.get(Tenant, run.tenant_id)).external_id == external_id
        assert (
            await task_continuations.get_continuation(db, idempotency_key=pending.idempotency_key)
            is None
        )
