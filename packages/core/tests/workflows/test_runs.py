"""Runs against the database: start, the step seam, retries, recovery and the ledger."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from daimon.core._models import (
    SecurityAuditEvent,
    Tenant,
    WorkflowRun,
    WorkflowRunEvent,
    WorkflowStep,
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.stores import workflow_runs, workflows
from daimon.core.stores.workflow_run_events import list_events
from daimon.core.workflows import engine, preflight, runs
from daimon.core.workflows.ledger import verify
from daimon.core.workflows.preflight import Refusal
from daimon.core.workflows.runs import Start, StepWork
from daimon.testing.factories import make_account, make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
Sessions = async_sessionmaker[AsyncSession]


def make_settings(**workflows_settings: Any) -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
            "features": {"workflows": "on"},
            "workflows": workflows_settings,
        }
    )


INLINE: dict[str, Any] = {
    "b": {"kind": "branch", "cases": [{"when": "${{ inputs.n > 1 }}", "to": "t"}], "else": "f"},
    "t": {"kind": "transform", "set": {"double": "${{ inputs.n * 2 }}"}, "next": "f"},
    "f": {"kind": "finish", "output": "${{ has(steps.t) ? steps.t.output : {} }}"},
}
AGENT: dict[str, Any] = {
    "a": {"kind": "agent", "instructions": "Do it", "next": "f"},
    "f": {"kind": "finish", "output": "${{ steps.a.output }}"},
}


@dataclass
class World:
    sm: Sessions
    settings: Settings
    tenant_id: uuid.UUID
    owner: uuid.UUID
    workflow_id: uuid.UUID

    def start(self, **kw: Any) -> Start:
        kw.setdefault("run_as_account_id", self.owner)
        return Start(self.workflow_id, triggered_by={"kind": "person"}, **kw)

    async def run(self, **kw: Any) -> workflow_runs.RunRow:
        run = await runs.start_run(
            self.sm, self.settings, tenant_id=self.tenant_id, start=self.start(**kw), now=NOW
        )
        assert not isinstance(run, Refusal), f"the run starts: {run}"
        return run

    async def get(self, run_id: uuid.UUID) -> workflow_runs.RunRow:
        async with self.sm() as s:
            run = await workflow_runs.get_run(s, run_id)
        assert run is not None
        return run

    async def steps(self, run_id: uuid.UUID) -> dict[str, workflow_runs.StepRow]:
        async with self.sm() as s:
            return {
                engine.step_key(r.node_id, r.visit): r
                for r in await workflow_runs.list_steps(s, run_id)
            }

    async def claim(self, run_id: uuid.UUID, key: str, now: datetime = NOW) -> runs.Claimed:
        step = (await self.steps(run_id))[key]
        claimed = await runs.claim(
            self.sm, step_id=step.id, owner="w1", lease=timedelta(minutes=5), now=now
        )
        assert claimed is not None, f"{key} is claimed"
        return claimed

    async def execute(
        self, claimed: runs.Claimed, *outcomes: engine.Outcome, now: datetime = NOW
    ) -> None:
        queue = list(outcomes)

        async def fake(work: StepWork) -> engine.Outcome:
            assert work.attempt == claimed.attempt, "the executor sees its attempt"
            return queue.pop(0)

        await runs.execute(self.sm, self.settings, {"agent": fake}, claimed, clock=lambda: now)

    async def verified(self, run_id: uuid.UUID) -> list[str]:
        async with self.sm() as s:
            events = await list_events(s, run_id)
        run = await self.get(run_id)
        result = verify(run_id, events)
        assert result.ok and result.head == run.head_hash, f"the ledger verifies: {result}"
        return [e.type for e in events]


async def make_world(
    db_session: AsyncSession, nodes: dict[str, Any], start: str, **settings: Any
) -> World:
    tenant = await make_tenant(db_session)
    owner = await make_account(db_session, tenant=tenant)
    await db_session.execute(
        update(Tenant).where(Tenant.id == tenant.id).values(funding_mode="operator_funded")
    )
    spec = {"schema": "daimon.workflow/v1", "name": "T", "start": start, "nodes": nodes}
    flow = await workflows.create_workflow(
        db_session, tenant_id=tenant.id, owner_account_id=owner.id, name="T", spec=spec
    )
    await workflows.publish_version(
        db_session,
        tenant_id=tenant.id,
        workflow_id=flow.id,
        spec=spec,
        spec_hash="h",
        source="web",
        published_by=owner.id,
    )
    await db_session.commit()
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    return World(sm, make_settings(**settings), tenant.id, owner.id, flow.id)


async def test_a_manual_run_goes_through_branch_transform_finish(db_session: AsyncSession) -> None:
    world = await make_world(db_session, INLINE, "b")
    run = await world.run(inputs={"n": 3})
    assert (run.status, run.outcome) == ("succeeded", "completed"), f"inline nodes finish: {run}"
    steps = await world.steps(run.id)
    assert steps["f"].output == {"double": 6}, "Finish renders its output"
    types = await world.verified(run.id)
    assert (
        types[:3] == ["run.queued", "run.started", "step.ready"] and types[-1] == "run.succeeded"
    ), types
    audit = await db_session.scalar(
        select(SecurityAuditEvent.reason).where(SecurityAuditEvent.tool_name == "workflow_run")
    )
    assert audit is not None and str(run.head_hash) in audit, (
        "the finished head reaches the audit log"
    )


async def test_preflight_refuses_before_anything_is_created(db_session: AsyncSession) -> None:
    world = await make_world(db_session, INLINE, "b")
    world.settings = world.settings.model_copy(update={"features": {}})
    refused = await runs.start_run(
        world.sm, world.settings, tenant_id=world.tenant_id, start=world.start(), now=NOW
    )
    assert isinstance(refused, Refusal) and refused.reason == "workflows_off", (
        f"feature off: {refused}"
    )
    assert await db_session.scalar(select(WorkflowRun.id)) is None, "a refusal creates no run"
    bad = await runs.start_run(
        world.sm,
        make_settings(),
        tenant_id=world.tenant_id,
        start=world.start(run_as_account_id=uuid.uuid4()),
        now=NOW,
    )
    assert isinstance(bad, Refusal) and bad.reason == "run_as_not_allowed", f"unknown run-as: {bad}"


async def test_executor_seam_retries_then_succeeds(db_session: AsyncSession) -> None:
    world = await make_world(db_session, AGENT, "a")
    run = await world.run()
    assert (await world.steps(run.id))["a"].status == "ready", (
        "the agent step waits for the steps loop"
    )
    await world.execute(await world.claim(run.id, "a"), engine.Failed("retryable", "overloaded"))
    step = (await world.steps(run.id))["a"]
    assert step.status == "ready" and step.ready_at == NOW + timedelta(seconds=20), (
        f"backoff: {step}"
    )
    later = NOW + timedelta(seconds=30)
    async with world.sm() as s:
        due = await workflow_runs.ready_steps(s, now=later, limit=5)
    assert [d[0] for d in due] == [step.id], "the retry is due after its backoff"
    await world.execute(
        await world.claim(run.id, "a", later), engine.Succeeded({"x": 1}), now=later
    )
    done = await world.get(run.id)
    assert done.status == "succeeded", f"the second attempt finishes the run: {done}"
    types = await world.verified(run.id)
    assert types.count("step.retry_scheduled") == 1, types
    async with world.sm() as s:
        attempts = await workflow_runs.list_attempts(s, step.id)
    assert [a.status for a in attempts] == ["failed", "succeeded"], "one attempt row each"


async def test_a_missing_executor_fails_the_step_fatally(db_session: AsyncSession) -> None:
    world = await make_world(db_session, AGENT, "a")
    run = await world.run()
    claimed = await world.claim(run.id, "a")
    await runs.execute(world.sm, world.settings, {}, claimed, clock=lambda: NOW)
    failed = await world.get(run.id)
    assert (failed.status, failed.failure_reason) == ("failed", "step_failed"), f"{failed}"
    assert (await world.steps(run.id))["a"].error_class == "fatal", "no executor is fatal"


async def test_cancel_makes_the_running_settle_stale(db_session: AsyncSession) -> None:
    world = await make_world(db_session, AGENT, "a")
    run = await world.run()
    claimed = await world.claim(run.id, "a")
    assert await runs.cancel_run(
        world.sm, world.settings, run_id=run.id, account_id=world.owner, now=NOW
    )
    settled = await runs.settle(
        world.sm,
        world.settings,
        step_id=claimed.step.id,
        generation=claimed.step.generation,
        outcome=engine.Succeeded(),
        now=NOW,
    )
    assert not settled, "a cancelled step's late result is refused"
    after = await world.get(run.id)
    assert (after.status, after.failure_reason) == ("cancelled", "cancelled_by"), f"{after}"
    assert (await world.steps(run.id))["a"].status == "cancelled", "the step is cancelled"
    await world.verified(run.id)


async def test_redrive_reruns_the_failed_step_on_the_same_run(db_session: AsyncSession) -> None:
    world = await make_world(db_session, AGENT, "a")
    run = await world.run()
    await world.execute(await world.claim(run.id, "a"), engine.Failed("agent", "no"))
    assert (await world.get(run.id)).status == "failed", "the agent failure fails the run"
    redriven = await runs.redrive(
        world.sm, world.settings, run_id=run.id, account_id=world.owner, now=NOW
    )
    assert not isinstance(redriven, Refusal) and redriven.status == "running", f"{redriven}"
    await world.execute(await world.claim(run.id, "a"), engine.Succeeded({"x": 2}))
    done = await world.get(run.id)
    assert (done.status, done.redrive_count, done.failure_reason) == ("succeeded", 1, None), (
        f"{done}"
    )
    assert "run.redriven" in await world.verified(run.id), "the redrive is in the ledger"


async def test_fork_copies_upstream_outputs_and_starts_at_the_node(
    db_session: AsyncSession,
) -> None:
    nodes = {
        "a": {"kind": "agent", "instructions": "One", "next": "b"},
        "b": {"kind": "agent", "instructions": "Two", "next": "f"},
        "f": {"kind": "finish", "output": "${{ steps.b.output }}"},
    }
    world = await make_world(db_session, nodes, "a")
    source = await world.run()
    await world.execute(await world.claim(source.id, "a"), engine.Succeeded({"x": 1}))
    fork = await world.run(fork_of=(source.id, "b"))
    steps = await world.steps(fork.id)
    assert (steps["a"].reused, steps["a"].output) == ("fork", {"x": 1}), "a is copied"
    assert steps["b"].status == "ready" and fork.forked_from_run_id == source.id, f"{steps}"
    assert "run.forked_from" in await world.verified(fork.id), "the fork names its source"


async def test_lost_worker_after_a_send_is_unknown_until_the_owner_decides(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, AGENT, "a")
    run = await world.run()
    claimed = await world.claim(run.id, "a")
    async with world.sm.begin() as s:
        await workflow_runs.mark_send_intent(s, step_id=claimed.step.id, now=NOW)
    late = NOW + timedelta(minutes=10)
    async with world.sm() as s:
        expired = await workflow_runs.expired_leases(s, now=late, limit=5)
    assert expired == [claimed.step.id], "the lease ran out"
    await runs.reclaim(world.sm, world.settings, step_id=claimed.step.id, now=late)
    waiting = await world.get(run.id)
    assert (waiting.status, waiting.waiting_reason) == ("waiting", "unknown"), f"{waiting}"
    decided = await runs.decide_unknown(
        world.sm,
        world.settings,
        step_id=claimed.step.id,
        decision="skip",
        account_id=world.owner,
        now=late,
        output={"x": 3},
    )
    done = await world.get(run.id)
    assert decided and done.status == "succeeded", f"skip with an output finishes: {done}"
    types = await world.verified(run.id)
    assert {"step.unknown", "step.resolved_by", "run.waiting"} <= set(types), types


async def test_lost_worker_before_a_send_runs_again(db_session: AsyncSession) -> None:
    world = await make_world(db_session, AGENT, "a")
    run = await world.run()
    claimed = await world.claim(run.id, "a")
    await runs.reclaim(
        world.sm, world.settings, step_id=claimed.step.id, now=NOW + timedelta(minutes=10)
    )
    step = (await world.steps(run.id))["a"]
    assert step.status == "ready" and step.generation == claimed.step.generation, f"retried: {step}"


async def test_a_gate_refusal_waits_blocked_then_resumes(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await make_world(db_session, AGENT, "a")
    run = await world.run()
    claimed = await world.claim(run.id, "a")

    async def depleted(*_: object, **__: object) -> str:
        return "balance_depleted"

    monkeypatch.setattr(preflight, "spend_gate", depleted)
    await world.execute(claimed, engine.Succeeded())
    blocked = await world.get(run.id)
    assert (blocked.status, blocked.waiting_reason) == ("waiting", "blocked"), f"{blocked}"
    monkeypatch.undo()
    later = NOW + timedelta(minutes=20)
    async with world.sm() as s:
        [wait] = await workflow_runs.due_waits(s, kind="blocked", now=later, limit=5)
    await runs.recheck_blocked(world.sm, world.settings, wait, now=later)
    assert (await world.steps(run.id))["a"].status == "ready", "the cleared gate readies the step"
    assert (await world.get(run.id)).status == "running", "the run resumes"


async def test_a_blocked_step_fails_with_the_gate_reason_after_a_day(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await make_world(db_session, AGENT, "a")
    run = await world.run()

    async def capped(*_: object, **__: object) -> str:
        return "cap_exceeded"

    monkeypatch.setattr(preflight, "spend_gate", capped)
    await world.execute(await world.claim(run.id, "a"), engine.Succeeded())
    later = NOW + timedelta(days=1, minutes=1)
    async with world.sm() as s:
        [wait] = await workflow_runs.due_waits(s, kind="blocked", now=later, limit=5)
    await runs.recheck_blocked(world.sm, world.settings, wait, now=later)
    failed = await world.get(run.id)
    assert (failed.status, failed.failure_reason) == ("failed", "cap_exceeded"), f"{failed}"


async def test_test_runs_use_the_draft_and_pinned_outputs(db_session: AsyncSession) -> None:
    world = await make_world(db_session, AGENT, "a")
    run = await world.run(mode="test", pinned={"a": {"output": {"x": 9}}})
    assert (run.mode, run.status) == ("test", "succeeded"), (
        f"the pinned agent step is reused: {run}"
    )
    assert (await world.steps(run.id))["f"].output == {"x": 9}, "Finish reads the pinned output"


async def test_runs_over_the_active_limit_queue(db_session: AsyncSession) -> None:
    world = await make_world(db_session, AGENT, "a", max_active_runs=1)
    first = await world.run()
    second = await world.run()
    assert (first.status, second.status) == ("running", "queued"), "the second run queues"
    await world.execute(await world.claim(first.id, "a"), engine.Succeeded())
    assert await runs.promote_queued(world.sm, world.settings, second.id, now=NOW), "room again"
    assert (await world.get(second.id)).status == "running", "the queued run begins"


async def test_deadline_and_budget_end_the_run(db_session: AsyncSession) -> None:
    world = await make_world(db_session, AGENT, "a")
    run = await world.run()
    end = engine.RunEnd("timed_out", reason="deadline", message="The run passed its deadline")
    assert await runs.stop_run(world.sm, world.settings, run_id=run.id, end=end, now=NOW)
    assert (await world.get(run.id)).status == "timed_out", "the deadline times the run out"
    assert run.deadline_at == NOW + timedelta(days=7), "the default deadline is a week"
    assert run.budget_usd == 5, "the default run budget comes from the feature"


async def test_the_ledger_is_append_only(db_session: AsyncSession) -> None:
    world = await make_world(db_session, INLINE, "b")
    run = await world.run(inputs={"n": 0})
    for statement in (
        update(WorkflowRunEvent).where(WorkflowRunEvent.run_id == run.id).values(type="x"),
        delete(WorkflowRunEvent).where(WorkflowRunEvent.run_id == run.id),
    ):
        async with world.sm.begin() as s:
            with pytest.raises(DBAPIError, match="append-only"):
                await s.execute(statement)
            await s.rollback()
    async with world.sm.begin() as s:
        await workflow_runs.delete_runs(s, [run.id])
    assert await db_session.scalar(select(WorkflowRunEvent.seq)) is None, "a run delete cascades"
    assert await db_session.scalar(select(WorkflowStep.id)) is None, "with its steps"
