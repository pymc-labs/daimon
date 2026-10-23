"""A fleet the deadline ended with no delivered wake still tells its thread once."""

from __future__ import annotations

import uuid

from daimon.core._models import FleetRun as FleetRunRow
from daimon.core._models import TaskContinuation
from daimon.core.fleet_run_end import poll_run_end_notices_once
from daimon.core.stores import fleet, task_continuations
from sqlalchemy import func, update

from .test_worker_finished import _arm, _request, _run, _worker
from .test_worker_finished import pooled as pooled


async def _read(sm, key):
    async with sm() as db:
        return await task_continuations.get_continuation(db, idempotency_key=key)


async def _armed_run(sm):
    """A running run with one worker and its parked wake, as at J4's deadline."""
    run = await _run(sm)
    worker = await _worker(sm, run, 0)
    async with sm.begin() as db:
        wake = await _arm(db, run, [worker])
    return run, worker, wake


async def _expire(sm, run):
    async with sm.begin() as db:
        await db.execute(
            update(FleetRunRow)
            .where(FleetRunRow.id == run.id)
            .values(deadline_at=func.now() - func.make_interval(0, 0, 0, 0, 0, 1))
        )
    return await fleet.sweep_expired(sm, tenant_id=run.tenant_id)


class _Post:
    def __init__(self, *results: bool) -> None:
        self.results = list(results)
        self.calls: list[tuple[uuid.UUID, str, tuple[str, ...]]] = []

    async def __call__(self, run, workers):
        self.calls.append((run.id, run.status, tuple(w.status for w in workers)))
        return self.results.pop(0) if self.results else True


async def test_deadline_end_posts_one_notice_with_final_states(pooled):
    run, _, wake = await _armed_run(pooled)
    assert await _expire(pooled, run) == (1, 1)
    row = await _read(pooled, wake.idempotency_key)
    assert row.status == "cancelled" and row.skip_reason == "run_expired"

    post = _Post()
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 1
    assert post.calls == [(run.id, "failed", ("failed",))]
    row = await _read(pooled, wake.idempotency_key)
    assert row.skip_reason == task_continuations.RUN_EXPIRED_SHOWN
    # Once only: the next poll finds nothing owed.
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
    assert len(post.calls) == 1


async def test_failed_post_is_retried_on_the_next_poll(pooled):
    run, _, wake = await _armed_run(pooled)
    await _expire(pooled, run)
    post = _Post(False)
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
    assert (await _read(pooled, wake.idempotency_key)).skip_reason == "run_expired"
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 1
    assert len(post.calls) == 2


async def test_an_end_that_is_not_the_deadline_owes_no_notice(pooled):
    # A root turn or a Stop press ends the run itself and tells the thread.
    run, _, wake = await _armed_run(pooled)
    await fleet.set_run_status(
        pooled,
        tenant_id=run.tenant_id,
        run_id=run.id,
        expected_version=run.version,
        status="cancelled",
    )
    assert (await _read(pooled, wake.idempotency_key)).skip_reason == "run_ended"
    post = _Post()
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
    assert post.calls == []


async def test_other_platform_and_foreign_thread_rows_post_nothing(pooled):
    run, _, wake = await _armed_run(pooled)
    await _expire(pooled, run)
    post = _Post()
    assert await poll_run_end_notices_once(pooled, platform="slack", post=post) == 0
    # A withdrawn wake whose thread is not the run's own is settled, not shown.
    stray = _request(run).model_copy(
        update={
            "idempotency_key": uuid.uuid4(),
            "thread_id": "another-thread",
            "requested_work": f'{{"run_id":"{run.id}","workers":[]}}',
        }
    )
    async with pooled.begin() as db:
        await task_continuations.record_continuation(db, **stray.model_dump())
        await db.execute(
            update(TaskContinuation)
            .where(TaskContinuation.idempotency_key == stray.idempotency_key)
            .values(status="cancelled", skip_reason="run_expired")
        )
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 1
    assert [call[0] for call in post.calls] == [run.id]
    assert (await _read(pooled, stray.idempotency_key)).skip_reason == "run_expired_shown"
    assert (await _read(pooled, wake.idempotency_key)).skip_reason == "run_expired_shown"


async def test_broken_wait_body_still_names_its_run(pooled):
    run, _, _ = await _armed_run(pooled)
    corrupt = _request(run).model_copy(
        update={"idempotency_key": uuid.uuid4(), "requested_work": f'{{"run_id":"{run.id}",x'}
    )
    async with pooled.begin() as db:
        await task_continuations.record_continuation(db, **corrupt.model_dump())
    await _expire(pooled, run)
    assert (await _read(pooled, corrupt.idempotency_key)).skip_reason == "run_expired"
    post = _Post()
    # The corrupt row still names the run by its closed prefix, so it rides along.
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 1
    assert (await _read(pooled, corrupt.idempotency_key)).skip_reason == "run_expired_shown"


async def test_wait_text_naming_no_run_is_settled_without_a_post(pooled):
    run, _, _ = await _armed_run(pooled)
    loose = _request(run).model_copy(update={"idempotency_key": uuid.uuid4()})
    async with pooled.begin() as db:
        await task_continuations.record_continuation(db, **loose.model_dump())
        await db.execute(
            update(TaskContinuation)
            .where(TaskContinuation.idempotency_key == loose.idempotency_key)
            .values(status="cancelled", skip_reason="run_expired")
        )
    post = _Post()
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
    assert post.calls == []
    assert (await _read(pooled, loose.idempotency_key)).skip_reason == "run_expired_shown"
