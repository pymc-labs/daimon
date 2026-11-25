"""A routine node: its spec rules, live-only runs, and a reclaim that never reruns it."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from daimon.core._models import Account, WorkflowDelivery
from daimon.core.routines import PRIVATE_FAILED
from daimon.core.workflows import engine, runs, service
from daimon.core.workflows.access import Who
from daimon.core.workflows.preflight import routine_only
from daimon.core.workflows.runs import StepWork
from daimon.core.workflows.service import Env
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


async def _ends(world: World, outcome: engine.Outcome) -> uuid.UUID:
    async def step(_work: StepWork) -> engine.Outcome:
        return outcome

    run = await world.run()
    claimed = await world.claim(run.id, "r")
    await runs.execute(world.sm, world.settings, {"routine": step}, claimed, clock=lambda: NOW)
    return run.id


@pytest.mark.parametrize(
    ("to", "home", "hidden"),
    [(None, None, True), ({"kind": "dm"}, "555", True), (ROUTINE["r"]["to"], "555", False)],
    ids=["no_destination", "dm", "channel"],
)
async def test_routine_step_output_when_private_then_only_its_owner_reads_it(
    db_session: AsyncSession, to: dict[str, str] | None, home: str | None, hidden: bool
) -> None:
    """As `routines.has_private_output`: a DM or destination-free run is its owner's, not an
    admin's, in the run, its ledger, the attention list and its step's result."""
    node = {k: v for k, v in ROUTINE["r"].items() if k != "to"} | ({"to": to} if to else {})
    world = await make_world(db_session, ROUTINE | {"r": node}, "r", home=home, spec_fields=ONE)
    admin = Account(tenant_id=world.tenant_id)
    db_session.add(admin)
    await db_session.commit()
    readers = {
        "owner": (Env(world.sm, world.settings, Who(world.tenant_id, world.owner), NOW), True),
        "admin": (
            Env(world.sm, world.settings, Who(world.tenant_id, admin.id, admin=True), NOW),
            not hidden,
        ),
    }
    failed = await _ends(world, engine.Failed("fatal", "Your notes: boom"))
    for who, (env, shown) in readers.items():
        view = await service.get_run(env, failed)
        events, _ = await service.run_events(env, failed)
        texts = [view.failure.message if view.failure else None, view.steps[0].reason]
        texts += [e.detail for e in events if e.type in ("step.failed", "run.failed")]
        texts += [a.detail for a in await service.attention(env) if a.kind == "failed"]
        error = "Your notes: boom" if shown else PRIVATE_FAILED
        assert texts == [error] * 5, f"the run's error, for the {who}"
    succeeded = await _ends(world, engine.Succeeded({}, summary="Notes"))
    for who, (env, shown) in readers.items():
        found = await service.step(env, succeeded, "r", None)
        assert found.summary == ("Notes" if shown else None), f"the step's result, for the {who}"
