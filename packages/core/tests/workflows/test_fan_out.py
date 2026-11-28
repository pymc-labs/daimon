"""For each items and Workflow node child runs, against the database."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from daimon.core.stores import workflow_run_events, workflow_runs, workflow_waits
from daimon.core.workflows import engine, run_views, runs
from daimon.core.workflows.call_step import WorkflowStepExecutor
from daimon.core.workflows.preflight import Refusal
from daimon.testing.workflows import NOW, World, make_world
from sqlalchemy.ext.asyncio import AsyncSession


def each(concurrency: int = 2, tolerate: int = 0) -> dict[str, Any]:
    return {
        "each": {
            "kind": "for_each",
            "items": "${{ inputs.names }}",
            "as": "name",
            "do": {"kind": "agent", "instructions": "Look it up", "inputs": {"who": "${{ name }}"}},
            "concurrency": concurrency,
            "tolerate_failures": tolerate,
            "next": "f",
        },
        "f": {"kind": "finish", "output": "${{ steps.each.output }}"},
    }


NAMES = ["Acme", {"name": "Globex"}, "Initech"]


async def statuses(world: World, run_id: uuid.UUID) -> list[str]:
    items = await world.items(run_id)
    return [items[f"each[{i}]"].status for i in range(len(items))]


async def test_items_run_within_concurrency_and_output_in_item_order(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, each(), "each")
    run = await world.run(inputs={"names": NAMES})
    assert await statuses(world, run.id) == ["ready", "ready", "pending"], "two at once"
    second = await world.claim(run.id, "each[1]")
    assert second.step.lane and second.step.lane.endswith("@each[1]"), "each item has a lane"
    await world.execute(second, engine.Succeeded({"n": 1}))
    assert await statuses(world, run.id) == ["ready", "succeeded", "ready"], "the next starts"
    await world.execute(await world.claim(run.id, "each[0]"), engine.Succeeded({"n": 0}))
    await world.execute(await world.claim(run.id, "each[2]"), engine.Succeeded({"n": 2}))
    done = await world.get(run.id)
    assert done.status == "succeeded", f"{done}"
    steps = await world.steps(run.id)
    assert steps["f"].output == [{"n": 0}, {"n": 1}, {"n": 2}], "outputs in item order"
    assert steps["each"].summary == "3 of 3 items succeeded", steps["each"].summary
    async with world.sm() as s:
        events = await workflow_run_events.list_events(s, run.id)
    started = next(e for e in events if e.type == "step.started" and e.step_key == "each")
    assert started.data == {"attempt": 1, "total": 3}, "progress reads the total"
    item = next(e for e in events if e.type == "step.started" and e.step_key == "each[1]")
    assert item.data.get("lane") == second.step.lane, f"an item starts in its lane: {item.data}"
    ready = next(e for e in events if e.type == "step.ready" and e.step_key == "each[1]")
    assert ready.data["item"] == {"index": 1, "label": "Globex"}, "items carry a label"
    await world.verified(run.id)


async def test_the_item_and_its_alias_reach_the_executor(db_session: AsyncSession) -> None:
    world = await make_world(db_session, each(), "each")
    run = await world.run(inputs={"names": NAMES})
    seen: list[Any] = []

    async def agent(work: runs.StepWork) -> engine.Outcome:
        seen.append((work.node.kind, work.context["name"], work.context["index"]))
        return engine.Succeeded()

    claimed = await world.claim(run.id, "each[1]")
    await runs.execute(world.sm, world.settings, {"agent": agent}, claimed, clock=lambda: NOW)
    assert seen == [("agent", {"name": "Globex"}, 1)], "the do node runs with its item"
    async with world.sm() as s:
        shown = await run_views.step_detail(s, run, "each[1]", None, now=NOW)
    assert shown is not None and shown.input == {"who": {"name": "Globex"}}, f"{shown}"


async def test_too_many_failures_fail_the_for_each_and_cancel_the_rest(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, each(concurrency=3, tolerate=1), "each")
    run = await world.run(inputs={"names": NAMES})
    await world.execute(await world.claim(run.id, "each[0]"), engine.Failed("agent", "no"))
    assert (await world.get(run.id)).status == "running", "one failure is tolerated"
    await world.execute(await world.claim(run.id, "each[2]"), engine.Failed("agent", "nope"))
    failed = await world.get(run.id)
    assert failed.status == "failed" and "2 of 3 items failed" in (failed.failure_message or "")
    assert await statuses(world, run.id) == ["failed", "cancelled", "failed"], "the rest stop"
    await world.verified(run.id)
    redriven = await runs.redrive(
        world.sm, world.settings, run_id=run.id, account_id=world.owner, now=NOW
    )
    assert not isinstance(redriven, Refusal), f"{redriven}"
    assert await statuses(world, run.id) == ["ready", "ready", "ready"], "redrive reruns items"


async def test_redrive_keeps_items_that_succeeded(db_session: AsyncSession) -> None:
    world = await make_world(db_session, each(concurrency=3), "each")
    run = await world.run(inputs={"names": NAMES})
    await world.execute(await world.claim(run.id, "each[0]"), engine.Succeeded({"n": 0}))
    await world.execute(await world.claim(run.id, "each[1]"), engine.Failed("agent", "no"))
    assert (await world.get(run.id)).status == "failed"
    await runs.redrive(world.sm, world.settings, run_id=run.id, account_id=world.owner, now=NOW)
    assert await statuses(world, run.id) == ["succeeded", "ready", "ready"], "the rest rerun"


async def test_cancelling_the_run_cancels_its_items(db_session: AsyncSession) -> None:
    world = await make_world(db_session, each(), "each")
    run = await world.run(inputs={"names": NAMES})
    await world.claim(run.id, "each[0]")
    await runs.cancel_run(world.sm, world.settings, run_id=run.id, account_id=world.owner, now=NOW)
    assert set(await statuses(world, run.id)) == {"cancelled"}, "every item stops"
    await world.verified(run.id)


async def test_items_that_are_not_a_list_fail_the_step(db_session: AsyncSession) -> None:
    world = await make_world(db_session, each(), "each")
    run = await world.run(inputs={"names": "Acme"})
    failed = await world.get(run.id)
    assert failed.status == "failed", f"{failed}"
    assert (await world.steps(run.id))["each"].error_message == "The items are not a list"


CHILD: dict[str, Any] = {
    "a": {"kind": "agent", "instructions": "Child", "next": "f"},
    "f": {"kind": "finish", "output": "${{ {'got': inputs.x} }}"},
}


def caller(child: uuid.UUID) -> dict[str, Any]:
    return {
        "call": {
            "kind": "workflow",
            "workflow": str(child),
            "inputs": {"x": "${{ inputs.x }}"},
            "next": "f",
        },
        "f": {"kind": "finish", "output": "${{ steps.call.output }}"},
    }


async def call_world(db_session: AsyncSession) -> tuple[World, uuid.UUID]:
    world = await make_world(db_session, {"f": {"kind": "finish"}}, "f")
    child = await world.add_workflow(CHILD, "a")
    world.workflow_id = await world.add_workflow(caller(child), "call")
    return world, child


async def run_call(world: World, run_id: uuid.UUID) -> uuid.UUID:
    claimed = await world.claim(run_id, "call")
    executor = WorkflowStepExecutor(world.sm, world.settings, clock=lambda: NOW)
    await runs.execute(world.sm, world.settings, {"workflow": executor}, claimed, clock=lambda: NOW)
    step = (await world.steps(run_id))["call"]
    assert (step.status, step.waiting_reason) == ("waiting", "event"), "the parent waits"
    async with world.sm() as s:
        child = await workflow_runs.latest_child(s, step.id)
    assert child is not None and child.parent_run_id == run_id, "the child links to its parent"
    return child.id


async def test_a_workflow_step_waits_on_its_child_and_takes_its_output(
    db_session: AsyncSession,
) -> None:
    world, _ = await call_world(db_session)
    run = await world.run(inputs={"x": 7})
    child_id = await run_call(world, run.id)
    await world.execute(await world.claim(child_id, "a"), engine.Succeeded())
    assert (await world.get(child_id)).status == "succeeded"
    assert await runs.sync_children(world.sm, world.settings, now=NOW) == 1
    done = await world.get(run.id)
    assert done.status == "succeeded", f"{done}"
    assert (await world.steps(run.id))["f"].output == {"got": 7}, "the child's output"
    assert "wait.decided" in await world.verified(run.id)


async def test_a_failed_child_fails_the_workflow_step(db_session: AsyncSession) -> None:
    world, _ = await call_world(db_session)
    run = await world.run(inputs={"x": 1})
    child_id = await run_call(world, run.id)
    await world.execute(await world.claim(child_id, "a"), engine.Failed("fatal", "broke"))
    await runs.sync_children(world.sm, world.settings, now=NOW)
    failed = await world.get(run.id)
    assert failed.status == "failed", f"{failed}"
    assert (await world.steps(run.id))["call"].error_class == "agent", "the child's failure"


async def test_cancelling_the_parent_cancels_the_child(db_session: AsyncSession) -> None:
    world, _ = await call_world(db_session)
    run = await world.run(inputs={"x": 1})
    child_id = await run_call(world, run.id)
    await runs.cancel_run(world.sm, world.settings, run_id=run.id, account_id=world.owner, now=NOW)
    assert await runs.sync_children(world.sm, world.settings, now=NOW) == 1
    child = await world.get(child_id)
    assert (child.status, child.failure_reason) == ("cancelled", "parent_stopped"), f"{child}"


async def test_calls_stop_past_the_depth_limit(db_session: AsyncSession) -> None:
    world = await make_world(db_session, {"f": {"kind": "finish"}}, "f")
    flow = await world.add_workflow({"f": {"kind": "finish"}}, "f")
    world.workflow_id = await world.add_workflow(caller(flow), "call")
    run = await world.run(
        inputs={"x": 1}, trigger_kind="call", triggered_by={"kind": "call", "depth": 3}
    )
    claimed = await world.claim(run.id, "call")
    executor = WorkflowStepExecutor(world.sm, world.settings, clock=lambda: NOW)
    await runs.execute(world.sm, world.settings, {"workflow": executor}, claimed, clock=lambda: NOW)
    step = (await world.steps(run.id))["call"]
    assert step.status == "failed" and "3 deep" in (step.error_message or ""), f"{step}"


async def test_items_cancelled_by_too_many_failures_close_their_asks(
    db_session: AsyncSession,
) -> None:
    """The For each fails over to `on_error` and the run goes on; an item that was waiting
    on an ask closes it, so no one answers it and it never comes due."""
    nodes = each() | {"after": {"kind": "agent", "instructions": "Tidy up", "next": "f"}}
    nodes["each"] |= {"on_error": "after"}
    world = await make_world(db_session, nodes, "each")
    run = await world.run(inputs={"names": NAMES})
    claimed = await world.claim(run.id, "each[0]")

    async def ask(work: runs.StepWork) -> runs.Park:
        return runs.Park("approval", addressees=("owner",), expires_at=NOW + timedelta(days=1))

    await runs.execute(world.sm, world.settings, {"agent": ask}, claimed, clock=lambda: NOW)
    await world.execute(await world.claim(run.id, "each[1]"), engine.Failed("agent", "no"))
    assert (await world.get(run.id)).status == "running", "the run goes on past the For each"
    async with world.sm() as s:
        asked = await workflow_waits.waits_of_step(s, claimed.step.id)
    assert [w.status for w in asked] == ["cancelled"], f"the item's ask closes: {asked}"
