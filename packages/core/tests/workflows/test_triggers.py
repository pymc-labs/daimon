"""Triggers against the database: schedule slots, event triggers and concurrency."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from daimon.core._models import (
    Tenant,
    WorkflowRun,
    WorkflowTrigger,
    WorkflowTriggerEvent,
    WorkflowVersion,
)
from daimon.core.stores import workflow_triggers, workflows
from daimon.core.workflows import runs, triggers
from daimon.core.workflows.preflight import Refusal
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_runs import INLINE, make_settings

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
HOURLY = {"id": "hourly", "kind": "schedule", "cron": "0 * * * *", "with": {"n": 3}}


async def _publish(
    db: AsyncSession, tenant_id: uuid.UUID, owner: uuid.UUID, **spec: Any
) -> uuid.UUID:
    full = {"schema": "daimon.workflow/v1", "name": "T", "start": "b", "nodes": INLINE, **spec}
    flow = await workflows.create_workflow(
        db, tenant_id=tenant_id, owner_account_id=owner, name="T", spec=full
    )
    await workflows.publish_version(
        db,
        tenant_id=tenant_id,
        workflow_id=flow.id,
        spec=full,
        spec_hash="h",
        source="web",
        published_by=owner,
        now=NOW,
    )
    return flow.id


async def _workspace(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await make_tenant(db)
    owner = await make_account(db, tenant=tenant)
    await db.execute(
        update(Tenant).where(Tenant.id == tenant.id).values(funding_mode="operator_funded")
    )
    return tenant.id, owner.id


async def test_a_schedule_fires_its_slot_once_and_starts_a_run(db_session: AsyncSession) -> None:
    tenant_id, owner = await _workspace(db_session)
    flow = await _publish(db_session, tenant_id, owner, triggers=[HOURLY])
    await db_session.commit()
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    slot = NOW + timedelta(hours=1)
    async with sm() as s:
        assert await workflow_triggers.next_fires(s, workflow_ids=[flow]) == {flow: slot}, "seeded"
    assert await triggers.fire_schedules(sm, now=slot - timedelta(seconds=1)) == 0, "not yet"
    assert await triggers.fire_schedules(sm, now=slot + timedelta(seconds=5)) == 1, "the slot"
    assert await triggers.fire_schedules(sm, now=slot + timedelta(seconds=9)) == 0, "only once"
    started = await triggers.start_from_events(sm, make_settings(), now=slot)
    run = await db_session.scalar(select(WorkflowRun).where(WorkflowRun.workflow_id == flow))
    assert started == 1 and run is not None and run.inputs == {"n": 3}, "`with` are the inputs"
    assert run.trigger_kind == "schedule" and run.mode == "live", run.trigger_kind
    trigger = await db_session.scalar(select(WorkflowTrigger))
    assert trigger is not None and trigger.last_fired_slot == slot, "the slot is recorded"
    assert trigger.next_fire_at == slot + timedelta(hours=1), "the next slot follows"


async def test_a_missed_slot_is_skipped_unless_catch_up_runs_once(
    db_session: AsyncSession,
) -> None:
    tenant_id, owner = await _workspace(db_session)
    await _publish(db_session, tenant_id, owner, triggers=[HOURLY])
    await _publish(db_session, tenant_id, owner, triggers=[{**HOURLY, "catch_up": "run-once"}])
    await db_session.commit()
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    late = NOW + timedelta(hours=3, minutes=30)
    assert await triggers.fire_schedules(sm, now=late) == 1, "only the catch-up trigger fires"
    assert await triggers.fire_schedules(sm, now=late + timedelta(minutes=1)) == 0, "once"
    slots = set(await db_session.scalars(select(WorkflowTrigger.next_fire_at)))
    assert slots == {NOW + timedelta(hours=4)}, "both move past the gap"


async def test_a_republished_schedule_keeps_its_slot_and_a_dropped_one_stops(
    db_session: AsyncSession,
) -> None:
    tenant_id, owner = await _workspace(db_session)
    flow = await _publish(db_session, tenant_id, owner, triggers=[HOURLY])
    full = {"schema": "daimon.workflow/v1", "name": "T", "start": "b", "nodes": INLINE}
    later = NOW + timedelta(minutes=30)
    slot: datetime | None = None
    for spec in ({**full, "triggers": [HOURLY]}, full):
        await workflows.publish_version(
            db_session,
            tenant_id=tenant_id,
            workflow_id=flow,
            spec=spec,
            spec_hash="h",
            source="web",
            published_by=owner,
            now=later,
        )
        slot = await db_session.scalar(select(WorkflowTrigger.next_fire_at))
        if "triggers" in spec:
            assert slot == NOW + timedelta(hours=1), "the same cron keeps its slot"
    assert slot is None, "a dropped schedule stops"


async def test_a_finished_run_starts_the_workflow_listening_for_it(
    db_session: AsyncSession,
) -> None:
    tenant_id, owner = await _workspace(db_session)
    source = await _publish(db_session, tenant_id, owner)
    event = {
        "id": "after",
        "kind": "event",
        "source": f"workflow:{source}",
        "match": "event.run.status == 'succeeded'",
        "inputs": {"n": "${{ event.run.output.double }}"},
    }
    listener = await _publish(db_session, tenant_id, owner, triggers=[event])
    await db_session.commit()
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    settings = make_settings()
    start = runs.Start(
        source, triggered_by={"kind": "person"}, run_as_account_id=owner, inputs={"n": 2}
    )
    first = await runs.start_run(sm, settings, tenant_id=tenant_id, start=start, now=NOW)
    assert not isinstance(first, Refusal) and first.status == "succeeded", first
    assert await triggers.start_from_events(sm, settings, now=NOW) == 1, "the event starts one"
    run = await db_session.scalar(select(WorkflowRun).where(WorkflowRun.workflow_id == listener))
    assert run is not None and run.inputs == {"n": 4}, "the mapping reads the finished run"
    assert run.untrusted_inputs and run.triggered_by["depth"] == 1, run.triggered_by
    status = await db_session.scalar(select(WorkflowTriggerEvent.status))
    assert status == "started", status


async def test_skip_refuses_a_second_run_with_the_same_key(db_session: AsyncSession) -> None:
    tenant_id, owner = await _workspace(db_session)
    agent = {
        "a": {"kind": "agent", "instructions": "Do it", "next": "f"},
        "f": {"kind": "finish"},
    }
    full = {
        "schema": "daimon.workflow/v1",
        "name": "T",
        "start": "a",
        "nodes": agent,
        "concurrency": {"key": "${{ inputs.who }}", "on_conflict": "skip"},
    }
    flow = await workflows.create_workflow(
        db_session, tenant_id=tenant_id, owner_account_id=owner, name="T", spec=full
    )
    await workflows.publish_version(
        db_session,
        tenant_id=tenant_id,
        workflow_id=flow.id,
        spec=full,
        spec_hash="h",
        source="web",
        published_by=owner,
        now=NOW,
    )
    await db_session.commit()
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)

    async def start(who: str) -> object:
        go = runs.Start(
            flow.id, triggered_by={"kind": "person"}, run_as_account_id=owner, inputs={"who": who}
        )
        return await runs.start_run(sm, make_settings(), tenant_id=tenant_id, start=go, now=NOW)

    assert not isinstance(await start("ann"), Refusal), "the first runs"
    again = await start("ann")
    assert isinstance(again, Refusal) and again.reason == "skipped", again
    assert not isinstance(await start("bob"), Refusal), "another key runs"


async def _webhook_event(
    db: AsyncSession, tenant_id: uuid.UUID, flow: uuid.UUID, inputs: dict[str, Any], at: datetime
) -> None:
    trigger = await workflow_triggers.find_trigger(
        db, tenant_id=tenant_id, workflow_id=flow, key="hook"
    )
    assert trigger is not None, "publishing made the trigger"
    await workflow_triggers.record_event(
        db, trigger=trigger, status="accepted", received_at=at, verified=True, inputs=inputs
    )


async def test_an_event_that_cannot_start_is_rejected_and_the_batch_goes_on(
    db_session: AsyncSession,
) -> None:
    """A payload too deep for the concurrency key and a version that no longer parses each
    fail their own event; the batch's other event still starts, and the loop lives."""
    tenant_id, owner = await _workspace(db_session)
    hook = {"id": "hook", "kind": "webhook", "verify": "bearer"}
    keyed = await _publish(
        db_session, tenant_id, owner, triggers=[hook], concurrency={"key": "${{ inputs.n }}"}
    )
    broken = await _publish(db_session, tenant_id, owner, triggers=[hook])
    deep: dict[str, Any] = {"n": 1}
    for _ in range(3000):
        deep = {"n": deep}
    await _webhook_event(db_session, tenant_id, keyed, deep, NOW)
    await _webhook_event(db_session, tenant_id, broken, {"n": 1}, NOW + timedelta(seconds=1))
    await _webhook_event(db_session, tenant_id, keyed, {"n": 2}, NOW + timedelta(seconds=2))
    await db_session.execute(
        update(WorkflowVersion)
        .where(WorkflowVersion.workflow_id == broken)
        .values(spec={"schema": "daimon.workflow/v1"})
    )
    await db_session.commit()
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)

    assert await triggers.start_from_events(sm, make_settings(), now=NOW) == 1, "one starts"
    marks = (
        await db_session.execute(
            select(WorkflowTriggerEvent.status, WorkflowTriggerEvent.reason).order_by(
                WorkflowTriggerEvent.received_at
            )
        )
    ).all()
    assert [tuple(m) for m in marks] == [
        ("rejected", "invalid_inputs"),
        ("rejected", "failed"),
        ("started", None),
    ], marks


async def test_an_event_whose_mark_was_lost_is_not_started_twice(db_session: AsyncSession) -> None:
    """A crash after the run starts but before its event is marked leaves the event accepted;
    the next tick marks it with the run it already started."""
    tenant_id, owner = await _workspace(db_session)
    hook = {"id": "hook", "kind": "webhook", "verify": "bearer"}
    flow = await _publish(db_session, tenant_id, owner, triggers=[hook])
    await _webhook_event(db_session, tenant_id, flow, {}, NOW)
    await db_session.commit()
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    assert await triggers.start_from_events(sm, make_settings(), now=NOW) == 1, "it starts"
    await db_session.execute(update(WorkflowTriggerEvent).values(status="accepted", run_id=None))
    await db_session.commit()

    assert await triggers.start_from_events(sm, make_settings(), now=NOW) == 0, "no new run"
    runs = (await db_session.scalars(select(WorkflowRun.id))).all()
    event = (await db_session.scalars(select(WorkflowTriggerEvent))).one()
    assert len(runs) == 1, f"one run for one event: {runs}"
    assert (event.status, event.run_id) == ("started", runs[0]), "marked with its run"
