"""Runs against the database: start, the step seam, retries, recovery and the ledger."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core import usage_recording
from daimon.core._models import (
    Account,
    SecurityAuditEvent,
    UsageEvent,
    WorkflowRun,
    WorkflowRunEvent,
    WorkflowStep,
)
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.pricing import MODEL_PRICING
from daimon.core.stores import tenant_ledger, workflow_runs
from daimon.core.stores import workflows as workflows_store
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.workflows import engine, preflight, run_views, runs, service
from daimon.core.workflows.access import Who
from daimon.core.workflows.preflight import Refusal
from daimon.core.workflows.service import Env
from daimon.core.workflows.spec import parse_spec
from daimon.testing.workflows import NOW, World, make_settings, make_world
from mux.contracts.config import BackendConfig
from mux.testing.drivers.anthropic.ma_models import ma_model_usage
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

INLINE: dict[str, Any] = {
    "b": {"kind": "branch", "cases": [{"when": "${{ inputs.n > 1 }}", "to": "t"}], "else": "f"},
    "t": {"kind": "transform", "set": {"double": "${{ inputs.n * 2 }}"}, "next": "f"},
    "f": {"kind": "finish", "output": "${{ has(steps.t) ? steps.t.output : {} }}"},
}
AGENT: dict[str, Any] = {
    "a": {"kind": "agent", "instructions": "Do it", "next": "f"},
    "f": {"kind": "finish", "output": "${{ steps.a.output }}"},
}


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


async def test_specs_keep_their_key_order_and_ledger_rows_read_in_words(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, INLINE, "b")
    run = await world.run(inputs={"n": 3})
    async with world.sm() as s:
        version = await workflows_store.get_version(
            s, tenant_id=world.tenant_id, workflow_id=world.workflow_id, number=1
        )
        rows = await run_views.event_views(s, run)
    assert version is not None and list(version.spec["nodes"]) == ["b", "t", "f"], (
        "the stored spec keeps the author's node order"
    )
    details = {e.type: e.detail for e in rows}
    assert (details["run.queued"], details["run.started"]) == ("Started by hand", "Version 1"), (
        details
    )


async def test_a_step_given_all_untrusted_inputs_marks_each_field(
    db_session: AsyncSession,
) -> None:
    nodes = {
        "a": {"kind": "agent", "instructions": "Read", "inputs": {"lead": "${{ inputs }}"}},
    }
    world = await make_world(db_session, nodes, "a")
    run = await world.run(inputs={"name": "Ana", "email": "a@b.co"}, untrusted_inputs=True)
    await world.claim(run.id, "a")
    async with world.sm() as s:
        shown = await run_views.step_detail(s, run, "a", None, now=NOW)
    assert shown is not None and shown.untrusted == ["input.lead.name", "input.lead.email"]


async def test_the_latest_plan_note_is_the_step_checklist(db_session: AsyncSession) -> None:
    world = await make_world(db_session, {"a": {"kind": "agent", "instructions": "Do"}}, "a")
    run = await world.run()
    step = (await world.claim(run.id, "a")).step
    async with world.sm.begin() as s:
        for text in ("[>] Read", "Halfway", "[x] Read\n[>] Write"):
            await workflow_runs.add_activity(
                s, tenant_id=world.tenant_id, step_id=step.id, kind="note", text=text, now=NOW
            )
        shown = await run_views.step_detail(s, run, "a", None, now=NOW)
    assert shown is not None and shown.plan == [
        {"text": "Read", "status": "done"},
        {"text": "Write", "status": "active"},
    ]
    assert [a.text for a in shown.activity] == ["Halfway"], "plans leave the activity lines"


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


async def test_a_home_channel_on_another_backend_refuses_the_run(db_session: AsyncSession) -> None:
    world = await make_world(db_session, INLINE, "b", home="c1")
    openai = BackendConfig(backend="openai", profile="openai.persistent_workspace", model="gpt-5")
    await set_channel_backend(db_session, channel_ref(world.tenant_id, "discord", "c1"), openai)
    await db_session.commit()
    refused = await runs.start_run(
        world.sm, world.settings, tenant_id=world.tenant_id, start=world.start(), now=NOW
    )
    assert isinstance(refused, Refusal) and refused.reason == "backend_unsupported", refused


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
    async with world.sm() as s:
        state = run_views.project(await run_views.event_views(s, done))
    [shown] = [x for x in state.steps if x.key == "a"]
    first = shown.attempts[0].error
    assert first is not None and first["class"] == "retryable", "the attempt shows why it failed"


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


async def test_only_an_editor_skips_an_unknown_step_with_an_output(
    db_session: AsyncSession,
) -> None:
    """The skip's output feeds later steps, so a runner may retry or fail but not skip."""
    world = await make_world(db_session, AGENT, "a")
    env = await _runner(db_session, world)
    run = await world.run(triggered_by={"kind": "person", "account_id": str(env.who.account_id)})
    claimed = await world.claim(run.id, "a")
    async with world.sm.begin() as s:
        await workflow_runs.mark_send_intent(s, step_id=claimed.step.id, now=NOW)
    late = NOW + timedelta(minutes=10)
    await runs.reclaim(world.sm, world.settings, step_id=claimed.step.id, now=late)
    with pytest.raises(service.Forbidden, match="skip"):
        await service.decide(env, run.id, "skip", {"x": 3})
    failed = await service.decide(env, run.id, "fail")
    assert failed.status == "failed", f"a runner can fail their own run's step: {failed}"


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


async def test_a_test_run_does_not_compete_with_the_live_run_for_its_slot(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, AGENT, "a", max_active_runs=1)
    live = await world.run()
    test = await world.run(mode="test")
    assert (live.status, test.status) == ("running", "running"), "the test run starts beside it"
    queued = await world.run(mode="test")
    assert queued.status == "queued", "test runs still queue behind each other"


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


async def test_lane_usage_is_tagged_and_rolled_up_to_step_and_run(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, AGENT, "a")
    run = await world.run()
    claimed = await world.claim(run.id, "a")
    async with world.sm.begin() as s:
        attempt = await workflow_runs.running_attempt(s, claimed.step.id)
        assert attempt is not None, "the claim opened an attempt"
        await workflow_runs.set_attempt_session(
            s, attempt_id=attempt.id, session_id="ses_l", now=NOW
        )
    for event_id in ("evt_1", "evt_1", "evt_2"):  # a replay rolls up once
        await usage_recording.record_turn_usage(
            sessionmaker=world.sm,
            tenant_id=world.tenant_id,
            platform_user_id=None,
            managed_session_id="ses_l",
            model_id="claude-opus-4-7",
            event=BetaManagedAgentsSpanModelRequestEndEvent(
                id=event_id,
                is_error=False,
                model_request_start_id="start_1",
                model_usage=ma_model_usage(input_tokens=1000, output_tokens=500),
                processed_at=NOW + timedelta(minutes=1),
                type="span.model_request_end",
            ),
            pricing=MODEL_PRICING["claude-opus-4-7"],
            workflow_run_id=run.id,
        )
    async with world.sm() as s:
        tags = (
            await s.execute(
                select(UsageEvent.workflow_run_id, UsageEvent.workflow_step_id).distinct()
            )
        ).all()
        debits = await tenant_ledger.get_balance(s, tenant_id=world.tenant_id)
    assert tags == [(run.id, claimed.step.id)], f"usage rows name the run and step: {tags}"
    step = (await world.steps(run.id))["a"]
    total = (await world.get(run.id)).cost_usd
    assert step.cost_usd == total == -debits > 0, f"step and run carry the debits: {step}"


async def test_private_reach_follows_who_can_read_the_limited_channel(
    db_session: AsyncSession,
) -> None:
    deliver = {"f": {"kind": "finish", "deliver": {"to": "C_PRIVATE", "message": "hi"}}}
    world = await make_world(db_session, deliver, "f", home="C_OPEN")
    await set_access_policy(
        db_session,
        tenant_id=world.tenant_id,
        policy=TenantAccessPolicy(channel_rules={"C_PRIVATE": ChannelRule(readers="inside")}),
    )
    await db_session.commit()
    async with world.sm() as session:
        row = await workflows_store.get_workflow(
            session, tenant_id=world.tenant_id, workflow_id=world.workflow_id
        )
    assert row is not None, "the world's workflow"
    place = preflight.Place(
        world.tenant_id, row, parse_spec(row.draft_spec), 0, "live", world.owner
    )
    asked: list[str] = []

    def reads(member: bool) -> preflight.MayRead:
        async def may_read(_account: uuid.UUID, channel: str) -> bool:
            asked.append(channel)
            return member

        return may_read

    reached = await preflight.private_reach(db_session, place, world.owner, reads(True))
    assert (reached, asked) == (None, ["C_PRIVATE"]), "a member may; open channels ask nothing"
    refused = await preflight.private_reach(db_session, place, world.owner, reads(False))
    assert refused == "C_PRIVATE", "someone who cannot read the channel is refused, admins too"


async def test_a_fork_onto_the_live_version_runs_the_live_version(
    db_session: AsyncSession,
) -> None:
    nodes = {
        "a": {"kind": "agent", "instructions": "One", "next": "f"},
        "f": {"kind": "finish"},
    }
    world = await make_world(db_session, nodes, "a")
    source = await world.run()
    await world.execute(await world.claim(source.id, "a"), engine.Succeeded({"x": 1}))
    flow = await workflows_store.get_workflow(
        db_session, tenant_id=world.tenant_id, workflow_id=world.workflow_id
    )
    assert flow is not None
    await workflows_store.publish_version(
        db_session,
        tenant_id=world.tenant_id,
        workflow_id=world.workflow_id,
        spec=flow.draft_spec,
        spec_hash="h2",
        source="web",
        published_by=world.owner,
    )
    await db_session.commit()
    env = Env(world.sm, world.settings, Who(world.tenant_id, world.owner), NOW)
    forked = await service.rerun(env, source.id, step="f")
    live = await service.rerun(env, source.id, step="f", version="live")
    assert (forked.version, live.version) == (1, 2), "a fork keeps its source's unless told"


async def _runner(db_session: AsyncSession, world: World) -> Env:
    """A person with the runner role on the shared workflow."""
    runner = Account(tenant_id=world.tenant_id)
    db_session.add(runner)
    await db_session.commit()
    async with world.sm.begin() as s:
        await workflows_store.update_settings(
            s, tenant_id=world.tenant_id, workflow_id=world.workflow_id, visibility="shared"
        )
        await workflows_store.set_access(
            s,
            tenant_id=world.tenant_id,
            workflow_id=world.workflow_id,
            role="runner",
            granted_by=world.owner,
            account_id=runner.id,
        )
    return Env(world.sm, world.settings, Who(world.tenant_id, runner.id), NOW)


async def test_a_runner_reruns_only_the_live_version(db_session: AsyncSession) -> None:
    """Rerunning in test mode needs edit rights, as starting a test run does."""
    world = await make_world(db_session, {"f": {"kind": "finish"}}, "f")
    env = await _runner(db_session, world)
    source = await world.run(triggered_by={"kind": "person", "account_id": str(env.who.account_id)})
    with pytest.raises(service.Forbidden, match="owner or an editor"):
        await service.rerun(env, source.id, version="draft")
    again = await service.rerun(env, source.id)
    assert not again.test, "a plain rerun runs the live version"
