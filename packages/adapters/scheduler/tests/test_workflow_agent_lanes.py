"""Agent steps run through the loops and the scheduler's lane turn, with fake MA sessions."""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.adapters.scheduler.workflows import (
    HeadlessLaneTurn,
    WorkflowEngine,
    lane_turn,
    owner_dm,
    run_once,
)
from daimon.core.direct_messages import DirectMessageAccessRevoked
from daimon.core.headless_runner import StepTurn
from daimon.core.stores import accounts, spend_allowances, tenants, workflow_run_events
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.workflows import runs, step_tools
from daimon.core.workflows.agent_step import AgentStepExecutor
from daimon.testing.factories import make_platform_principal
from daimon.testing.workflows import NOW, World, make_world
from mux.testing.drivers.anthropic.ma_models import ma_model_usage
from sqlalchemy.ext.asyncio import AsyncSession

Act = Callable[[AsyncSession, step_tools.Grant, datetime], Awaitable[str]]


def complete(**output: Any) -> Act:
    return lambda s, g, now: step_tools.complete(
        s, g, output=output or None, summary="done", route=None, reason=None, now=now
    )


def early(s: AsyncSession, g: step_tools.Grant, now: datetime) -> Awaitable[str]:
    return step_tools.finish_early(s, g, summary="nothing new", now=now)


async def run_lanes(
    world: World, monkeypatch: pytest.MonkeyPatch, acts: list[Act], tokens: int = 1000
) -> tuple[uuid.UUID, list[str | None]]:
    """Run to the end; each fake session bills one model call, then does its step's act."""
    ticks = iter(range(1000))

    def clock() -> datetime:
        return NOW + timedelta(seconds=next(ticks))

    sessions: list[str | None] = []

    async def fake_run_turn(*, step: StepTurn, session_id: str | None, **kw: Any) -> str:
        assert kw["account_id"] == world.owner, "the turn runs as the run-as person"
        assert kw["tool_safety"].unattended_writes == (), "writes default to refuse"
        sessions.append(session_id)
        sid = session_id or f"ses_{len(sessions)}"
        await step.on_session(sid)
        record = kw["usage_record_factory"](sid, "claude-opus-4-7")
        await record(
            event=BetaManagedAgentsSpanModelRequestEndEvent(
                id=f"evt_{len(sessions)}",
                is_error=False,
                model_request_start_id="start",
                model_usage=ma_model_usage(input_tokens=tokens, output_tokens=100),
                processed_at=clock(),
                type="span.model_request_end",
            )
        )
        found = re.search(r'step="([0-9a-f-]{36})"', kw["trigger_message"])
        assert found, "the brief names the step"
        async with world.sm.begin() as s:
            g = await step_tools.grant(
                s,
                attempt_id=uuid.UUID(found.group(1)),
                account_id=world.owner,
                tenant_id=world.tenant_id,
                lock=True,
            )
            await acts.pop(0)(s, g, clock())
        return "ok"

    async def resolved(*_a: Any, **_kw: Any) -> Any:
        return SimpleNamespace(environment_name=None)

    monkeypatch.setattr(lane_turn, "run_turn", fake_run_turn)
    monkeypatch.setattr(lane_turn, "resolve_agent", AsyncMock(return_value="agent_1"))
    monkeypatch.setattr(lane_turn, "resolve_environment", AsyncMock(return_value="env_1"))
    monkeypatch.setattr(lane_turn, "resolve", resolved)
    turn = HeadlessLaneTurn(
        SimpleNamespace(),  # type: ignore[arg-type]
        world.sm,
        world.settings,
        SimpleNamespace(agent_name=None),  # type: ignore[arg-type]
        {},  # type: ignore[arg-type]
    )
    executor = AgentStepExecutor(world.sm, world.settings, turn, clock=clock)
    engine = WorkflowEngine(world.sm, world.settings, {"agent": executor}, owner="w1", clock=clock)
    run = await world.run()
    for _ in range(10):
        await run_once(engine)
        if (await world.get(run.id)).status != "running":
            break
    return run.id, sessions


async def test_agent_then_finish_runs_metered_through_the_loops(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await make_world(
        db_session,
        {
            "a": {
                "kind": "agent",
                "instructions": "Say hi",
                "output": {"greeting": {"type": "string"}},
                "next": "f",
            },
            "f": {"kind": "finish", "output": "${{ steps.a.output }}"},
        },
        "a",
    )
    run_id, _ = await run_lanes(world, monkeypatch, [complete(greeting="hi")])
    done, step = await world.get(run_id), (await world.steps(run_id))["a"]
    assert (done.status, step.output) == ("succeeded", {"greeting": "hi"}), f"{done}"
    assert step.cost_usd == done.cost_usd > 0, "the session's cost is on step and run"
    await world.verified(run_id)


DIGEST: dict[str, Any] = {
    "collect": {
        "kind": "agent",
        "instructions": "List the risky deals",
        "output": {"risky": {"type": "integer"}},
        "can_finish_early": True,
        "next": "check",
    },
    "check": {
        "kind": "branch",
        "cases": [{"when": "${{ steps.collect.output.risky > 0 }}", "to": "write"}],
        "else": "quiet",
    },
    "write": {
        "kind": "agent",
        "instructions": "Write the digest",
        "output": {"digest": {"type": "string"}},
        "next": "done",
    },
    "done": {"kind": "finish", "output": "${{ steps.write.output }}"},
    "quiet": {"kind": "finish"},
}


@pytest.mark.parametrize(
    ("acts", "outcome", "sessions"),
    [
        ([complete(risky=1), complete(digest="Acme")], "completed", [None, "ses_1"]),
        ([complete(risky=0)], "nothing_to_do", [None]),
        ([early], "nothing_to_do", [None]),
    ],
    ids=["digest", "quiet", "early"],
)
async def test_the_digest_continues_its_lane_or_ends_with_nothing_to_do(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    acts: list[Act],
    outcome: str,
    sessions: list[str | None],
) -> None:
    world = await make_world(db_session, DIGEST, "collect")
    run_id, seen = await run_lanes(world, monkeypatch, list(acts))
    done, steps = await world.get(run_id), await world.steps(run_id)
    assert (done.status, done.outcome, seen) == ("succeeded", outcome, sessions), f"{done}"
    agent_costs = [s.cost_usd for s in steps.values() if s.lane and s.status == "succeeded"]
    assert all(c > 0 for c in agent_costs) and done.cost_usd == sum(agent_costs), f"{steps}"
    await world.verified(run_id)


async def test_step_spend_counts_against_the_run_as_persons_allowance_and_gates_them(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step usage is the run-as person's, as for any turn of theirs: their allowance counts
    it, and once it is used up their next step waits blocked on it."""
    world = await make_world(db_session, DIGEST, "collect")
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    owner = await accounts.get_account(db_session, world.owner)
    assert tenant is not None and owner is not None
    await make_platform_principal(
        db_session, platform=tenant.platform, external_id="U1", tenant=tenant, account=owner
    )
    await spend_allowances.create_allowance(
        db_session,
        tenant_id=world.tenant_id,
        subject_kind="person",
        subject_id="U1",
        agent_id=None,
        amount_usd=Decimal("0.01"),
        window="total",
        starts_at=NOW - timedelta(days=1),
    )
    await db_session.commit()
    run_id, seen = await run_lanes(
        world, monkeypatch, [complete(risky=1), complete(digest="x")], 10_000
    )
    used = await spend_allowances.used_since(
        db_session,
        tenant_id=world.tenant_id,
        platform_user_ids={"U1"},
        agent_id=None,
        since=NOW - timedelta(days=1),
        markup=world.settings.billing.markup,
    )
    done, steps = await world.get(run_id), await world.steps(run_id)
    assert used == done.cost_usd > 0, "the first step's spend counts against their allowance"
    assert (seen, steps["write"].waiting_reason) == ([None], "blocked"), "the next step waits"


@pytest.mark.parametrize("busy", [False, True])
async def test_an_owner_dm_step_runs_as_the_owners_dm_and_skips_when_they_are_busy(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, busy: bool
) -> None:
    nodes = {
        "run": {"kind": "agent", "agent": "daimon", "instructions": "Hi", "session": "owner_dm"},
        "done": {"kind": "finish"},
    }
    nodes["run"]["next"] = "done"
    world = await make_world(db_session, nodes, "run")
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    owner = await accounts.get_account(db_session, world.owner)
    assert tenant is not None and owner is not None
    await make_platform_principal(
        db_session, platform="discord", external_id="U1", tenant=tenant, account=owner
    )
    await set_dm_enabled(db_session, tenant_id=world.tenant_id, enabled=True)
    await db_session.commit()
    asked: list[tuple[str, str]] = []

    async def fake_dm(_deps: object, *, owner_user_id: str, prompt: str, **_: Any) -> object:
        asked.append((owner_user_id, prompt))
        if busy:
            raise DirectMessageAccessRevoked("owner_busy", message="busy")
        return SimpleNamespace(text="Your digest")

    monkeypatch.setattr(owner_dm, "run_dm_routine", fake_dm)
    monkeypatch.setattr(owner_dm, "owner_granted", AsyncMock(return_value=True))
    deps = SimpleNamespace(membership_resolver=None)
    executor = AgentStepExecutor(
        world.sm,
        world.settings,
        AsyncMock(),
        owner_dm=owner_dm.OwnerDmTurn(world.sm, world.settings, deps),  # type: ignore[arg-type]
    )
    run = await world.run()
    await runs.execute(
        world.sm,
        world.settings,
        {"agent": executor},
        await world.claim(run.id, "run"),
        clock=lambda: NOW,
    )
    done, step = await world.get(run.id), (await world.steps(run.id))["run"]
    assert asked == [("U1", "Hi")], "the owner's DM turn gets the instructions"
    assert done.status == "succeeded", done
    assert step.summary == ("Skipped: the owner was busy." if busy else "Your digest"), step
    async with world.sm() as s:
        events = await workflow_run_events.list_events(s, run.id)
    started = next(e for e in events if e.type == "step.started")
    assert (started.data.get("lane"), started.data.get("agent")) == ("owner_dm:daimon", "daimon"), (
        f"the owner DM lane is on step.started: {started.data}"
    )
