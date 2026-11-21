"""Polls against the database: trigger baselines and per-row runs, the pause on a changed
tool, a failing poll beside a healthy one, the Off feature, and poll waits."""

from __future__ import annotations

import ast
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from daimon.core._models import WorkflowDelivery, WorkflowRun, WorkflowTrigger
from daimon.core.config import Settings
from daimon.core.stores import workflow_runs, workflows
from daimon.core.workflows import polls, runs, triggers, waits
from daimon.core.workflows.preflight import Refusal
from daimon.testing.workflows import NOW, make_settings, make_world
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_runs import INLINE
from .test_triggers import _publish as publish_flow  # pyright: ignore[reportPrivateUsage]
from .test_triggers import _workspace  # pyright: ignore[reportPrivateUsage]

POLL: dict[str, Any] = {
    "id": "rows",
    "kind": "poll",
    "agent": "marketing",
    "tool": "notes/list_rows",
    "every": "5m",
    "items": ".results",
    "key": ".id",
    "inputs": {"n": "${{ item.n }}"},
}
MINUTE = timedelta(minutes=1)


def rows(*items: tuple[str, int, str]) -> dict[str, JsonValue]:
    return {"results": [{"id": i, "n": n, "status": s} for i, n, s in items]}


class Fake:
    """A poll client that answers its queued results (the last one repeats)."""

    def __init__(self, *results: JsonValue, digest: str = "d1") -> None:
        self.results, self.digest, self.calls = list(results), digest, 0
        self.error: Exception | None = None

    async def read(
        self, tenant_id: uuid.UUID, agent: str, tool: str, args: dict[str, JsonValue], /
    ) -> polls.Polled:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return polls.Polled(
            self.results.pop(0) if len(self.results) > 1 else self.results[0], self.digest
        )


async def _publish(
    db: AsyncSession, tenant_id: uuid.UUID, owner: uuid.UUID, **trigger: Any
) -> uuid.UUID:
    flow = await publish_flow(db, tenant_id, owner, triggers=[{**POLL, **trigger}])
    await db.commit()
    return flow


async def _pass(
    sm: async_sessionmaker[AsyncSession], client: Fake, at: Any, settings: Settings | None = None
) -> int:
    """One poll pass then the starts loop; returns the runs it started."""
    settings = settings or make_settings()
    await polls.poll_triggers(sm, settings, client, now=at)
    return await triggers.start_from_events(sm, settings, now=at)


async def test_the_first_poll_sets_a_baseline_and_each_new_row_starts_one_run(
    db_session: AsyncSession,
) -> None:
    tenant_id, owner = await _workspace(db_session)
    flow = await _publish(db_session, tenant_id, owner)
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    client = Fake(
        rows(("a", 2, "Todo")),
        rows(("a", 2, "Todo"), ("b", 3, "Todo"), ("c", 4, "Todo")),
    )
    assert await _pass(sm, client, NOW) == 0, "the baseline starts nothing"
    assert await _pass(sm, client, NOW + MINUTE) == 0 and client.calls == 1, "not due yet"
    assert await _pass(sm, client, NOW + 5 * MINUTE) == 2, "one run per new row"
    assert await _pass(sm, client, NOW + 10 * MINUTE) == 0, "a row starts once"
    found = list(
        await db_session.scalars(select(WorkflowRun).where(WorkflowRun.workflow_id == flow))
    )
    assert sorted(r.inputs["n"] for r in found) == [3, 4], "the mapping gives the inputs"
    assert all(r.trigger_kind == "poll" and r.untrusted_inputs for r in found), (
        "poll rows are untrusted"
    )


@pytest.mark.parametrize(("once", "expected"), [(False, 2), (True, 1)])
async def test_updated_rows_start_a_run_per_change_unless_once(
    db_session: AsyncSession, once: bool, expected: int
) -> None:
    tenant_id, owner = await _workspace(db_session)
    await _publish(
        db_session,
        tenant_id,
        owner,
        rule="updated",
        watch=[".status"],
        once=once,
        start_when="item.status != 'Todo'",
    )
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    client = Fake(rows(("a", 2, "Todo")), rows(("a", 2, "Doing")), rows(("a", 2, "Done")))
    started = 0
    for step in range(3):
        started += await _pass(sm, client, NOW + step * 5 * MINUTE)
    assert started == expected, f"once={once}: a changed watched field starts a run"


async def test_a_changed_tool_pauses_the_trigger_until_it_is_published_again(
    db_session: AsyncSession,
) -> None:
    tenant_id, owner = await _workspace(db_session)
    flow = await _publish(db_session, tenant_id, owner)
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    await _pass(sm, Fake(rows(("a", 2, "Todo"))), NOW)
    changed = Fake(rows(("a", 2, "Todo"), ("b", 3, "Todo")), digest="d2")
    assert await _pass(sm, changed, NOW + 5 * MINUTE) == 0, "nothing starts on a changed tool"
    trigger = await db_session.scalar(select(WorkflowTrigger))
    assert trigger is not None
    await db_session.refresh(trigger)
    assert not trigger.enabled and (trigger.state or {}).get("paused") == "source_changed", (
        trigger.state
    )
    notice = await db_session.scalar(
        select(WorkflowDelivery).where(WorkflowDelivery.kind == "notice")
    )
    assert notice is not None, "the owner is told"
    assert await _pass(sm, changed, NOW + 10 * MINUTE) == 0 and changed.calls == 1, (
        "paused is not polled"
    )
    spec = {
        "schema": "daimon.workflow/v1",
        "name": "T",
        "start": "b",
        "nodes": INLINE,
        "triggers": [POLL],
    }
    async with sm.begin() as s:
        await workflows.publish_version(
            s,
            tenant_id=tenant_id,
            workflow_id=flow,
            spec=spec,
            spec_hash="h2",
            source="web",
            published_by=owner,
            now=NOW + 11 * MINUTE,
        )
    await db_session.refresh(trigger)
    assert trigger.enabled and "paused" not in (trigger.state or {}), "publishing again wakes it"


async def test_a_failing_poll_does_not_stop_the_others(db_session: AsyncSession) -> None:
    tenant_id, owner = await _workspace(db_session)
    await _publish(db_session, tenant_id, owner, id="bad", agent="broken")
    await _publish(db_session, tenant_id, owner, id="good")
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)

    class Split:
        def __init__(self) -> None:
            self.good = Fake(rows(("a", 2, "Todo")))

        async def read(
            self, tenant_id: uuid.UUID, agent: str, tool: str, args: dict[str, JsonValue], /
        ) -> polls.Polled:
            if agent == "broken":
                raise RuntimeError("boom")
            return await self.good.read(tenant_id, agent, tool, args)

    await polls.poll_triggers(sm, make_settings(), Split(), now=NOW)
    states = {t.trigger_key: t.state for t in await workflow_triggers_all(db_session)}
    assert states["good"] and "seen" in states["good"], "the healthy poll recorded its baseline"
    assert not states["bad"], "the crashed poll changed nothing and the pass went on"


async def workflow_triggers_all(db: AsyncSession) -> list[WorkflowTrigger]:
    found = list(await db.scalars(select(WorkflowTrigger)))
    for t in found:
        await db.refresh(t)
    return found


async def test_a_poll_failure_is_noted_and_retried_an_interval_later(
    db_session: AsyncSession,
) -> None:
    tenant_id, owner = await _workspace(db_session)
    await _publish(db_session, tenant_id, owner)
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    client = Fake(rows(("a", 2, "Todo")))
    client.error = polls.PollFailed("The read failed (timeout)")
    await _pass(sm, client, NOW)
    trigger = (await workflow_triggers_all(db_session))[0]
    assert (trigger.state or {}).get("error") == "failed", trigger.state
    assert trigger.next_fire_at == NOW + 5 * MINUTE, "retried an interval later"


async def test_polls_off_stops_polling_and_refuses_runs(db_session: AsyncSession) -> None:
    tenant_id, owner = await _workspace(db_session)
    flow = await _publish(db_session, tenant_id, owner)
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    off = make_settings().model_copy(
        update={"features": {"workflows": "on", "workflow_polls": "off"}}
    )
    client = Fake(rows(("a", 2, "Todo")))
    await _pass(sm, client, NOW, off)
    assert client.calls == 0, "an Off feature never reads"
    refused = await runs.start_run(
        sm,
        off,
        tenant_id=tenant_id,
        start=runs.Start(
            flow, run_as_account_id=owner, triggered_by={"kind": "person"}, inputs={"n": 3}
        ),
        now=NOW,
    )
    assert isinstance(refused, Refusal) and refused.reason == "polls_off", refused


WAIT: dict[str, Any] = {
    "w": {
        "kind": "wait",
        "poll": {
            "agent": "marketing",
            "tool": "notes/get_status",
            "every": "2m",
            "args": {"id": "x"},
            "until": "result.status == 'done'",
        },
        "timeout": "10m",
        "routes": {"arrived": "f", "timed_out": "f"},
    },
    "f": {"kind": "finish", "output": "${{ steps.w.output }}"},
}


async def _parked(db_session: AsyncSession) -> tuple[Any, uuid.UUID]:
    world = await make_world(db_session, WAIT, "w")
    run = await world.run()
    claimed = await world.claim(run.id, "w")
    executors = {"wait": waits.WaitExecutor(clock=lambda: NOW)}
    await runs.execute(world.sm, world.settings, executors, claimed, clock=lambda: NOW)
    assert (await world.steps(run.id))["w"].waiting_reason == "poll", "a poll wait parks as poll"
    return world, run.id


async def test_a_poll_wait_keeps_checking_until_its_condition_holds(
    db_session: AsyncSession,
) -> None:
    world, run_id = await _parked(db_session)
    client = Fake({"status": "busy"}, {"status": "done"})
    assert await polls.poll_waits(world.sm, world.settings, client, now=NOW) == 0
    assert await polls.poll_waits(world.sm, world.settings, client, now=NOW + MINUTE) == 0, (
        "next check is 2m on"
    )
    assert client.calls == 1
    assert await polls.poll_waits(world.sm, world.settings, client, now=NOW + 2 * MINUTE) == 1
    steps = await world.steps(run_id)
    assert steps["f"].output == {"status": "done"}, "the last result is the wait's output"


async def test_a_poll_wait_times_out_on_its_route(db_session: AsyncSession) -> None:
    world, run_id = await _parked(db_session)
    client = Fake({"status": "busy"})
    late = NOW + 11 * MINUTE
    assert await polls.poll_waits(world.sm, world.settings, client, now=late) == 0
    assert client.calls == 0, "a wait past its timeout is not polled"
    async with world.sm() as s:
        due = await workflow_runs.due_waits(s, kind="poll", now=late, limit=10)
    await waits.on_due(world.sm, world.settings, due[0], now=late)
    assert "wait.timed_out" in await world.verified(run_id), "the timeout routes on"


async def test_a_paused_tool_fails_the_poll_wait(db_session: AsyncSession) -> None:
    world, run_id = await _parked(db_session)
    client = Fake({"status": "busy"})
    client.error = polls.PollPaused("tool_unlisted")
    await polls.poll_waits(world.sm, world.settings, client, now=NOW)
    assert (await world.get(run_id)).status == "failed", "a wait cannot go on without its tool"


def test_polls_only_record_events_and_the_client_decides_every_call() -> None:
    root = Path(polls.__file__).parent
    used = {
        n.attr if isinstance(n, ast.Attribute) else n.id
        for n in ast.walk(ast.parse((root / "polls.py").read_text()))
        if isinstance(n, ast.Attribute | ast.Name)
    }
    assert not {"start_run", "start"} & used, "a poll never starts a run itself"
    tree = ast.parse((root / "tool_client.py").read_text())
    listing = next(
        f for f in ast.walk(tree) if isinstance(f, ast.AsyncFunctionDef) and f.name == "_listing"
    )
    called = {
        c.func.id
        for c in ast.walk(listing)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }
    assert "decide_unattended_call" in called, "the poll client asks the one unattended decision"
