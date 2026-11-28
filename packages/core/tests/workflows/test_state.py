"""What a workflow keeps between runs: `workflow.state` and `workflow.last_run`."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from typing import Any

from daimon.core._models import Workflow, WorkflowRunEvent
from daimon.core.workflows import service
from daimon.core.workflows.access import Who
from daimon.core.workflows.service import Env
from daimon.core.workflows.state import STATE_LIMIT
from daimon.testing.db import build_test_engine
from daimon.testing.workflows import NOW, World, make_world
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

SAVES: dict[str, Any] = {
    "f": {
        "kind": "finish",
        "output": "${{ has(workflow.state.seen) ? workflow.state.seen : 0 }}",
        "state": {
            "seen": "${{ inputs.n }}",
            "gone": "${{ inputs.n > 1 ? null : 'x' }}",
            "item-${{ inputs.n }}": True,
        },
    }
}


async def _state(world: World) -> dict[str, Any]:
    async with world.sm() as s:
        flow = await s.scalar(select(Workflow).where(Workflow.id == world.workflow_id))
    assert flow is not None
    return flow.state


async def test_a_finish_merges_its_state_and_a_null_deletes_a_key(db_session: AsyncSession) -> None:
    world = await make_world(db_session, SAVES, "f")
    first = await world.run(inputs={"n": 1})
    assert (await world.steps(first.id))["f"].output == 0, "the first run sees no state"
    assert await _state(world) == {"seen": 1, "gone": "x", "item-1": True}, "written"
    second = await world.run(inputs={"n": 2})
    assert (await world.steps(second.id))["f"].output == 1, "the next run reads what was saved"
    assert await _state(world) == {"seen": 2, "item-1": True, "item-2": True}, (
        "merged at the top level; null removed `gone`"
    )
    assert "state.saved" in await world.verified(second.id), "the ledger records the save"
    async with world.sm() as s:
        data = await s.scalar(
            select(WorkflowRunEvent.data).where(
                WorkflowRunEvent.run_id == second.id, WorkflowRunEvent.type == "state.saved"
            )
        )
    assert data == {"keys": ["gone", "item-2", "seen"]}, "keys only, never the values"


async def test_a_state_over_the_cap_fails_the_run_and_changes_nothing(
    db_session: AsyncSession,
) -> None:
    nodes = {"f": {"kind": "finish", "state": {"big": "${{ inputs.text }}"}}}
    world = await make_world(db_session, nodes, "f")
    ok = await world.run(inputs={"text": "x"})
    assert ok.status == "succeeded", "a small state is saved"
    run = await world.run(inputs={"text": "y" * (STATE_LIMIT + 1)})
    assert (run.status, run.failure_reason) == ("failed", "state_too_large"), f"{run}"
    assert await _state(world) == {"big": "x"}, "the old state stands"
    types = await world.verified(run.id)
    assert "run.failed" in types and "run.succeeded" not in types, "one ending in the ledger"


async def test_two_finishes_at_once_both_land(db_session: AsyncSession, db_schema: str) -> None:
    nodes = {"f": {"kind": "finish", "state": {"${{ inputs.key }}": True}}}
    world = await make_world(db_session, nodes, "f")
    # Own connections: on the shared one the runs would never wait for each other's lock.
    engines = [
        build_test_engine(os.environ["DAIMON_DATABASE__TEST_URL"], db_schema, poolclass=NullPool)
        for _ in range(4)
    ]
    try:
        await asyncio.gather(
            *(
                replace(world, sm=async_sessionmaker(bind=e, expire_on_commit=False)).run(
                    inputs={"key": f"k{i}"}
                )
                for i, e in enumerate(engines)
            )
        )
    finally:
        for e in engines:
            await e.dispose()
    assert set(await _state(world)) == {"k0", "k1", "k2", "k3"}, "the row lock keeps every key"


async def test_test_runs_and_failed_runs_leave_the_state_alone(db_session: AsyncSession) -> None:
    nodes = {
        "b": {
            "kind": "branch",
            "cases": [{"when": "${{ inputs.n > 1 }}", "to": "bad"}],
            "else": "f",
        },
        "f": {"kind": "finish", "state": {"n": "${{ inputs.n }}"}},
        "bad": {"kind": "finish", "outcome": "failed", "state": {"n": "${{ inputs.n }}"}},
    }
    world = await make_world(db_session, nodes, "b")
    await world.run(inputs={"n": 1})
    await world.run(inputs={"n": 0}, mode="test")
    failed = await world.run(inputs={"n": 2})
    assert failed.status == "failed", "the run ended at the failed Finish"
    assert await _state(world) == {"n": 1}, "only the live success wrote"


async def test_last_run_is_the_latest_succeeded_live_run(db_session: AsyncSession) -> None:
    nodes = {
        "b": {
            "kind": "branch",
            "cases": [{"when": "${{ inputs.n > 1 }}", "to": "bad"}],
            "else": "f",
        },
        "f": {"kind": "finish", "output": {"prev": "${{ workflow.last_run }}"}},
        "bad": {"kind": "finish", "outcome": "failed"},
    }
    world = await make_world(db_session, nodes, "b")
    first = await world.run(inputs={"n": 1})
    assert (await world.steps(first.id))["f"].output == {"prev": None}, "none before any run"
    await world.run(inputs={"n": 0}, mode="test")
    await world.run(inputs={"n": 2})
    third = await world.run(inputs={"n": 1})
    prev = (await world.steps(third.id))["f"].output["prev"]
    assert (prev["number"], prev["output"], prev["posted"]) == (
        first.number,
        {"prev": None},
        [],
    ), f"the test and failed runs in between are skipped: {prev}"
    assert prev["started_at"] and prev["ended_at"], "times are there"


async def test_run_date_is_the_day_in_the_specs_timezone(db_session: AsyncSession) -> None:
    nodes = {
        "f": {"kind": "finish", "output": {"d": "${{ run.date }}", "t": "${{ run.started_at }}"}}
    }
    world = await make_world(db_session, nodes, "f", spec_fields={"timezone": "Pacific/Auckland"})
    run = await world.run()
    assert (await world.steps(run.id))["f"].output == {
        "d": "2026-11-21",
        "t": "2026-11-21T01:00:00+13:00",
    }, f"{NOW} is already the 21st in Auckland"


async def test_the_state_is_shown_to_viewers_and_cleared_by_editors(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, SAVES, "f")
    await world.run(inputs={"n": 1})
    env = Env(world.sm, world.settings, Who(world.tenant_id, world.owner, admin=False), NOW)
    shown = await service.saved_state(env, world.workflow_id)
    assert shown.state["seen"] == 1 and shown.updated_at, "the owner reads the state"
    await service.clear_state(env, world.workflow_id)
    assert await _state(world) == {}, "cleared"
