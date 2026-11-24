"""A routine node: its spec rules, live-only runs, and a reclaim that never reruns it."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from daimon.core._models import WorkflowDelivery
from daimon.core.workflows import engine, runs
from daimon.core.workflows.preflight import routine_only
from daimon.core.workflows.runs import StepWork
from daimon.core.workflows.spec import parse_spec
from daimon.core.workflows.validate import validate_spec
from daimon.testing.workflows import NOW, World, make_world
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

ROUTINE: dict[str, Any] = {
    "r": {
        "kind": "routine",
        "agent": "helper",
        "prompt": "Sum up the day",
        "to": {"kind": "channel", "id": "555"},
        "next": "f",
    },
    "f": {"kind": "finish"},
}
ONE = {"concurrency": {"limit": 1, "on_conflict": "skip"}}


def _codes(nodes: dict[str, Any], **fields: Any) -> list[str]:
    doc: dict[str, Any] = {
        "schema": "daimon.workflow/v1",
        "name": "T",
        "start": "r",
        "nodes": nodes,
    }
    doc |= fields
    return [p.code for p in validate_spec(parse_spec(doc)).errors if p.code.startswith("routine")]


def test_routine_rules_when_validated_then_one_step_run_once_and_alone() -> None:
    assert _codes(ROUTINE, **ONE) == [], "one routine step, limit 1 on skip, is valid"
    assert _codes(ROUTINE) == ["routine_concurrency"], "it never runs twice at a time"
    two = ROUTINE | {"s": ROUTINE["r"] | {"next": "f"}, "r": ROUTINE["r"] | {"next": "s"}}
    assert _codes(two, **ONE) == ["routine_count"], "a workflow has one routine step at most"
    loop = ROUTINE | {"b": {"kind": "branch", "cases": [{"when": "true", "to": "r"}], "else": "f"}}
    loop["r"] = ROUTINE["r"] | {"next": "b"}
    assert _codes(loop, **ONE) == ["routine_loop"], "it runs once a run"
    doc: dict[str, Any] = {"schema": "daimon.workflow/v1", "name": "T", "start": "r"}
    doc["nodes"] = ROUTINE
    assert routine_only(parse_spec(doc)), "a routine with a finish is routine-only work"


async def _unrun(work: StepWork) -> engine.Outcome:
    raise AssertionError(f"the routine step {work.step.node_id} never fires here")


async def _notices(world: World) -> int:
    async with world.sm() as s:
        return await s.scalar(select(func.count()).select_from(WorkflowDelivery)) or 0


async def test_routine_step_when_test_run_then_fails_live_only(db_session: AsyncSession) -> None:
    world = await make_world(db_session, ROUTINE, "r", home="555", spec_fields=ONE)
    run = await world.run(mode="test")
    claimed = await world.claim(run.id, "r")
    await runs.execute(world.sm, world.settings, {"routine": _unrun}, claimed, clock=lambda: NOW)
    step = (await world.steps(run.id))["r"]
    assert (step.status, step.error_message) == ("failed", runs.ROUTINE_LIVE_ONLY), f"{step}"
    assert (await world.get(run.id)).status == "failed", "the test run fails"


async def test_routine_step_when_reclaimed_then_fails_interrupted_without_notice(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, ROUTINE, "r", home="555", spec_fields=ONE)
    run = await world.run()
    claimed = await world.claim(run.id, "r")
    late = NOW + timedelta(minutes=10)
    await runs.reclaim(world.sm, world.settings, step_id=claimed.step.id, now=late)
    step = (await world.steps(run.id))["r"]
    assert (step.status, step.error_message) == ("failed", "Interrupted"), f"never rerun: {step}"
    assert (await world.get(run.id)).status == "failed", "the run fails, never waits as unknown"
    assert await _notices(world) == 0, "the engine adds no failure notice to a routine step's"
