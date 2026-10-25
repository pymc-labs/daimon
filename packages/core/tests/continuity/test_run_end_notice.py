"""An ended fleet run owes its thread a terminal card until one is shown there."""

from __future__ import annotations

import uuid

from daimon.core._models import FleetRun as FleetRunRow
from daimon.core._models import TaskContinuation
from daimon.core.continuity.wakes import claim_wake, settle_wake, start_wake
from daimon.core.fleet_run_end import poll_run_end_notices_once
from daimon.core.stores import fleet
from sqlalchemy import func, select, update

from .test_worker_finished import _NOW, _arm, _finish_batch, _run, _worker
from .test_worker_finished import pooled as pooled


async def _row(sm, run):
    async with sm() as db:
        return (await db.execute(select(FleetRunRow).where(FleetRunRow.id == run.id))).scalar_one()


async def _end(sm, run, status="failed"):
    return await fleet.set_run_status(
        sm, tenant_id=run.tenant_id, run_id=run.id, expected_version=run.version, status=status
    )


async def _due(sm, run):
    async with sm.begin() as db:
        await db.execute(
            update(FleetRunRow)
            .where(FleetRunRow.id == run.id)
            .values(end_notice_due_at=func.now() - func.make_interval(0, 0, 0, 0, 0, 0, 1))
        )


async def _ended(sm):
    run = await _run(sm)
    await _worker(sm, run, 0)
    run = await _end(sm, run)
    await _due(sm, run)
    return run


class _Post:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.notices = []

    async def __call__(self, notice):
        self.notices.append(notice)
        outcome = self.outcomes.pop(0) if self.outcomes else "shown"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_an_ended_run_is_owed_after_a_grace_and_shown_once(pooled):
    run = await _run(pooled)
    await _worker(pooled, run, 0)
    assert (await _row(pooled, run)).end_notice is None
    run = await _end(pooled, run)
    row = await _row(pooled, run)
    assert row.end_notice == "owed" and row.end_notice_due_at > row.finished_at
    post = _Post()
    # The turn that ended the run gets the grace to render its own card.
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
    assert post.notices == []

    await _due(pooled, run)
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 1
    (notice,) = post.notices
    assert notice.run.id == run.id and notice.run.status == "failed"
    assert [w.ordinal for w in notice.workers] == [0]
    assert notice.delivered is False and notice.card_message_id is None
    assert (await _row(pooled, run)).end_notice == "shown"
    await _due(pooled, run)
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
    assert len(post.notices) == 1


async def test_a_rendered_terminal_card_settles_the_notice(pooled):
    run = await _ended(pooled)
    await fleet.record_card(
        pooled, tenant_id=run.tenant_id, run_id=run.id, message_id="900", terminal=True
    )
    row = await _row(pooled, run)
    assert row.end_notice == "shown" and row.card_message_id == "900"
    post = _Post()
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0


async def test_a_live_card_is_remembered_for_the_notice(pooled):
    run = await _run(pooled)
    await _worker(pooled, run, 0)
    await fleet.record_card(
        pooled, tenant_id=run.tenant_id, run_id=run.id, message_id="901", terminal=False
    )
    run = await _end(pooled, run)
    await _due(pooled, run)
    post = _Post()
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 1
    assert post.notices[0].card_message_id == "901"


async def test_transient_failures_back_off_then_give_up(pooled):
    run = await _ended(pooled)
    post = _Post(*(["retry"] * fleet.END_NOTICE_MAX_ATTEMPTS))
    previous = None
    for attempt in range(1, fleet.END_NOTICE_MAX_ATTEMPTS + 1):
        assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
        row = await _row(pooled, run)
        assert row.end_notice == "owed" and row.end_notice_attempts == attempt
        if previous is not None:
            assert row.end_notice_due_at > previous
        # Not due again before its backoff.
        assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
        assert len(post.notices) == attempt
        previous = row.end_notice_due_at
        await _due(pooled, run)
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
    assert (await _row(pooled, run)).end_notice == "given_up"
    assert len(post.notices) == fleet.END_NOTICE_MAX_ATTEMPTS


async def test_a_permanent_failure_gives_up_at_once(pooled):
    run = await _ended(pooled)
    post = _Post("give_up")
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
    assert (await _row(pooled, run)).end_notice == "given_up"


async def test_one_runs_failure_does_not_hold_up_the_others(pooled):
    first = await _ended(pooled)
    second = await _ended(pooled)
    post = _Post(RuntimeError("boom"), "shown")
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 1
    states = {(await _row(pooled, run)).end_notice for run in (first, second)}
    assert states == {"owed", "shown"}


async def test_other_platforms_and_running_runs_are_not_claimed(pooled):
    run = await _run(pooled)
    await _ended(pooled)
    post = _Post()
    assert await poll_run_end_notices_once(pooled, platform="slack", post=post) == 0
    assert post.notices == []
    assert (await _row(pooled, run)).end_notice is None


async def test_a_wake_turn_running_at_the_end_is_waited_for(pooled):
    run = await _run(pooled)
    worker = await _worker(pooled, run, 0)
    async with pooled.begin() as db:
        wake = await _arm(db, run, [worker])
        assert await _finish_batch(db, run, worker)
    claim = await claim_wake(pooled, idempotency_key=wake.idempotency_key, now=_NOW)
    assert claim is not None and await start_wake(pooled, claim, now=_NOW)
    run, _ = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    run = await _end(pooled, run)
    await _due(pooled, run)
    post = _Post()
    # The started wake's turn may still answer and render the card itself.
    async with pooled.begin() as db:
        await db.execute(
            update(TaskContinuation)
            .where(TaskContinuation.idempotency_key == wake.idempotency_key)
            .values(started_at=func.now())
        )
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
    row = await _row(pooled, run)
    assert row.end_notice == "owed" and row.end_notice_attempts == 0
    assert post.notices == []
    # Past the wake turn's grace, the notice goes out.
    async with pooled.begin() as db:
        await db.execute(
            update(TaskContinuation)
            .where(TaskContinuation.idempotency_key == wake.idempotency_key)
            .values(started_at=func.now() - func.make_interval(0, 0, 0, 0, 1))
        )
    await _due(pooled, run)
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 1
    assert post.notices[0].delivered is False


async def test_a_delivered_wake_settles_the_card_without_a_notice_line(pooled):
    run = await _run(pooled)
    worker = await _worker(pooled, run, 0)
    async with pooled.begin() as db:
        wake = await _arm(db, run, [worker])
        assert await _finish_batch(db, run, worker)
    claim = await claim_wake(pooled, idempotency_key=wake.idempotency_key, now=_NOW)
    assert claim is not None and await start_wake(pooled, claim, now=_NOW)
    assert await settle_wake(pooled, claim, status="delivered", now=_NOW)
    run, _ = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    run = await _end(pooled, run, "cancelled")
    await _due(pooled, run)
    post = _Post()
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 1
    assert post.notices[0].delivered is True


async def test_settle_never_reopens_a_closed_notice(pooled):
    run = await _ended(pooled)
    await fleet.record_card(
        pooled, tenant_id=run.tenant_id, run_id=run.id, message_id="902", terminal=True
    )
    assert not await fleet.settle_end_notice(
        pooled, tenant_id=run.tenant_id, run_id=run.id, outcome="given_up"
    )
    assert (await _row(pooled, run)).end_notice == "shown"
    assert not await fleet.settle_end_notice(
        pooled, tenant_id=uuid.uuid4(), run_id=run.id, outcome="shown"
    )


async def test_a_run_nothing_was_spawned_into_owes_no_notice(pooled):
    # smoke-1: every turn holds a run and cancels it; it is not a fleet.
    run = await _run(pooled)
    run = await _end(pooled, run, "cancelled")
    row = await _row(pooled, run)
    assert row.finished_at is not None
    assert row.end_notice is None and row.end_notice_due_at is None
    post = _Post()
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
    assert post.notices == []


async def test_an_owed_run_without_workers_is_cleared_unposted(pooled):
    # Backstop for a row owed before the guard: claim clears it, posts nothing.
    run = await _run(pooled)
    run = await _end(pooled, run, "cancelled")
    async with pooled.begin() as db:
        await db.execute(
            update(FleetRunRow)
            .where(FleetRunRow.id == run.id)
            .values(end_notice="owed", end_notice_due_at=func.now())
        )
    await _due(pooled, run)
    post = _Post()
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
    assert post.notices == []
    row = await _row(pooled, run)
    assert row.end_notice is None and row.end_notice_attempts == 0


async def test_a_thread_s_live_run_card_is_found_until_the_run_ends(pooled):
    """A typed "stop the fleet" finds the thread's running run by its posted card."""

    async def live(run):
        return await fleet.live_run_card(
            pooled, tenant_id=run.tenant_id, platform="discord", thread_id="thread"
        )

    run = await _run(pooled)
    assert await live(run) is None, "no card posted yet"
    await fleet.record_card(
        pooled, tenant_id=run.tenant_id, run_id=run.id, message_id="901", terminal=False
    )
    assert await live(run) == (run.id, "901")
    assert (
        await fleet.live_run_card(
            pooled, tenant_id=run.tenant_id, platform="discord", thread_id="another-thread"
        )
        is None
    )
    await _end(pooled, run, status="cancelled")
    assert await live(run) is None


async def test_a_card_shown_with_a_cost_still_being_counted_owes_a_cost_update(pooled):
    """J10 rehearsal: the card said "Cost $0.01" while Astra's usage was still pending,
    and nothing ever edited it. Shown so, the run owes the card its settled cost."""
    run = await _ended(pooled)
    await fleet.record_card(
        pooled,
        tenant_id=run.tenant_id,
        run_id=run.id,
        message_id="902",
        terminal=True,
        cost_owed=True,
    )
    row = await _row(pooled, run)
    assert row.end_notice == "cost_owed" and row.card_message_id == "902"
    assert row.end_notice_attempts == 0
    await _due(pooled, run)
    # Still being counted: the poller retries with the backoff; no line, card only.
    post = _Post("retry")
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
    (notice,) = post.notices
    assert notice.cost_only and notice.delivered and notice.card_message_id == "902"
    assert (await _row(pooled, run)).end_notice == "cost_owed"
    # Settled: the card is edited with it, and the update is done.
    await _due(pooled, run)
    post = _Post("shown")
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 1
    assert (await _row(pooled, run)).end_notice == "shown"
    await _due(pooled, run)
    assert await poll_run_end_notices_once(pooled, platform="discord", post=_Post()) == 0


async def test_a_terminal_card_with_its_final_cost_settles_an_owed_cost_update(pooled):
    run = await _ended(pooled)
    for cost_owed, expected in ((True, "cost_owed"), (False, "shown")):
        await fleet.record_card(
            pooled,
            tenant_id=run.tenant_id,
            run_id=run.id,
            message_id="903",
            terminal=True,
            cost_owed=cost_owed,
        )
        assert (await _row(pooled, run)).end_notice == expected


async def test_an_end_posted_while_a_cost_is_counted_owes_its_card_the_cost(pooled):
    """The poller's own end post can find a cost still being counted too."""
    run = await _ended(pooled)
    post = _Post("cost_owed")
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 1
    row = await _row(pooled, run)
    assert row.end_notice == "cost_owed" and row.end_notice_attempts == 0
    await _due(pooled, run)
    post = _Post("shown")
    assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 1
    assert post.notices[0].cost_only
    assert (await _row(pooled, run)).end_notice == "shown"


async def test_a_cost_never_settled_keeps_its_card_honest_and_logs_once(pooled):
    import structlog

    run = await _ended(pooled)
    await fleet.record_card(
        pooled,
        tenant_id=run.tenant_id,
        run_id=run.id,
        message_id="904",
        terminal=True,
        cost_owed=True,
    )
    post = _Post(*(["retry"] * fleet.END_NOTICE_MAX_ATTEMPTS))
    for _ in range(fleet.END_NOTICE_MAX_ATTEMPTS):
        await _due(pooled, run)
        assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
    await _due(pooled, run)
    with structlog.testing.capture_logs() as logs:
        assert await poll_run_end_notices_once(pooled, platform="discord", post=post) == 0
    # The card keeps "cost so far"; the run owes nothing more; said once.
    assert (await _row(pooled, run)).end_notice == "shown"
    assert [e["event"] for e in logs].count("fleet.cost_update_given_up") == 1
    assert len(post.notices) == fleet.END_NOTICE_MAX_ATTEMPTS


async def test_two_processes_never_claim_the_same_cost_update(pooled):
    import asyncio

    run = await _ended(pooled)
    await fleet.record_card(
        pooled,
        tenant_id=run.tenant_id,
        run_id=run.id,
        message_id="905",
        terminal=True,
        cost_owed=True,
    )
    await _due(pooled, run)
    first, second = await asyncio.gather(
        fleet.claim_end_notices(pooled, platform="discord"),
        fleet.claim_end_notices(pooled, platform="discord"),
    )
    assert sorted((len(first), len(second))) == [0, 1]
