"""The workflow loops run a manual start to the end, through a fake agent executor."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from daimon.adapters.scheduler.workflows import WorkflowEngine, run_once
from daimon.adapters.scheduler.workflows.loops import polls_step
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.stores import tenants, workflow_runs, workflows
from daimon.core.stores.workflow_run_events import list_events
from daimon.core.workflows import engine, gate, polls, runs
from daimon.core.workflows.ledger import verify
from daimon.core.workflows.preflight import Refusal
from daimon.testing.factories import make_account, make_tenant
from pydantic import JsonValue, PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
NODES: dict[str, Any] = {
    "a": {"kind": "agent", "instructions": "Count", "next": "b"},
    "b": {
        "kind": "branch",
        "cases": [{"when": "${{ steps.a.output.n > 1 }}", "to": "t"}],
        "else": "f",
    },
    "t": {"kind": "transform", "set": {"double": "${{ steps.a.output.n * 2 }}"}, "next": "f"},
    "f": {"kind": "finish", "output": "${{ has(steps.t) ? steps.t.output : {} }}"},
}


async def _started(
    db_session: AsyncSession,
) -> tuple[async_sessionmaker[AsyncSession], Settings, uuid.UUID]:
    tenant = await make_tenant(db_session)
    owner = await make_account(db_session, tenant=tenant)
    await tenants.set_funding_mode(db_session, tenant_id=tenant.id, funding_mode="operator_funded")
    spec = {"schema": "daimon.workflow/v1", "name": "T", "start": "a", "nodes": NODES}
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
    settings = Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
            "features": {"workflows": "on"},
        }
    )
    start = runs.Start(flow.id, triggered_by={"kind": "person"}, run_as_account_id=owner.id)
    run = await runs.start_run(sm, settings, tenant_id=tenant.id, start=start, now=NOW)
    assert not isinstance(run, Refusal), f"the run starts: {run}"
    return sm, settings, run.id


async def test_a_manual_run_finishes_through_the_loops(db_session: AsyncSession) -> None:
    sm, settings, run_id = await _started(db_session)

    async def agent(work: runs.StepWork) -> engine.Outcome:
        return engine.Succeeded({"n": 3})

    await run_once(WorkflowEngine(sm, settings, {"agent": agent}, owner="w1", clock=lambda: NOW))

    async with sm() as session:
        done = await workflow_runs.get_run(session, run_id)
        steps = await workflow_runs.list_steps(session, run_id)
        events = await list_events(session, run_id)
    assert done is not None and (done.status, done.outcome) == ("succeeded", "completed"), done
    assert {s.node_id: s.output for s in steps}["f"] == {"double": 6}, "Finish renders its output"
    checked = verify(run_id, events)
    assert checked.ok and checked.head == done.head_hash, f"the ledger verifies: {checked}"


async def test_the_loops_stop_runs_without_spending_while_workflows_are_off(
    db_session: AsyncSession,
) -> None:
    sm, settings, run_id = await _started(db_session)
    off = settings.model_copy(update={"features": {"workflows": "off"}})
    called: list[runs.StepWork] = []

    async def agent(work: runs.StepWork) -> engine.Outcome:
        called.append(work)
        return engine.Succeeded({"n": 3})

    await run_once(WorkflowEngine(sm, off, {"agent": agent}, owner="w1", clock=lambda: NOW))
    async with sm() as session:
        held = await workflow_runs.get_run(session, run_id)
    assert not called, "no step spends while workflows are off"
    assert held is not None and held.status == "failed", held
    assert held.failure_message == gate.OFF, "the run says why it stopped"


class _Rows:
    """A poll client answering one row, then two; or crashing."""

    def __init__(self, *, crash: bool = False) -> None:
        self.crash, self.calls = crash, 0

    async def read(
        self, tenant_id: uuid.UUID, agent: str, tool: str, args: dict[str, JsonValue], /
    ) -> polls.Polled:
        self.calls += 1
        if self.crash:
            raise RuntimeError("boom")
        rows: list[JsonValue] = [{"id": "a"}]
        if self.calls > 1:
            rows.append({"id": "b"})
        return polls.Polled({"results": rows}, "d1")


async def _polling(
    db_session: AsyncSession,
) -> tuple[async_sessionmaker[AsyncSession], Settings, uuid.UUID]:
    sm, settings, run_id = await _started(db_session)  # a manual run to finish beside the polls
    tenant = await make_tenant(db_session)
    owner = await make_account(db_session, tenant=tenant)
    await tenants.set_funding_mode(db_session, tenant_id=tenant.id, funding_mode="operator_funded")
    poll = {
        "id": "rows", "kind": "poll", "agent": "marketing", "tool": "notes/list_rows",
        "every": "5m", "items": ".results", "key": ".id", "inputs": {"n": 3},
    }  # fmt: skip
    nodes = {"f": {"kind": "finish", "output": "${{ inputs.n }}"}}
    spec = {
        "schema": "daimon.workflow/v1",
        "name": "P",
        "start": "f",
        "nodes": nodes,
        "triggers": [poll],
    }
    flow = await workflows.create_workflow(
        db_session, tenant_id=tenant.id, owner_account_id=owner.id, name="P", spec=spec
    )
    await workflows.publish_version(
        db_session, tenant_id=tenant.id, workflow_id=flow.id, spec=spec, spec_hash="h",
        source="web", published_by=owner.id, now=NOW,
    )  # fmt: skip
    await db_session.commit()
    on = settings.model_copy(update={"features": {"workflows": "on", "workflow_polls": "on"}})
    return sm, on, run_id


async def test_the_polls_loop_starts_a_run_for_a_new_row(db_session: AsyncSession) -> None:
    sm, settings, _ = await _polling(db_session)
    client, now = _Rows(), [NOW]
    engine_ = WorkflowEngine(sm, settings, {}, owner="w1", clock=lambda: now[0], poll_client=client)
    await run_once(engine_)  # the baseline
    now[0] = NOW + timedelta(minutes=6)
    await run_once(engine_)
    await run_once(engine_)  # the starts loop takes the recorded event
    async with sm() as session:
        found = (
            await session.execute(
                text("SELECT count(*) FROM workflow_runs WHERE trigger_kind = 'poll'")
            )
        ).scalar_one()
    assert client.calls == 2 and found == 1, "one new row starts one run, through start_run"


async def test_a_crashing_poll_does_not_stop_the_other_loops(db_session: AsyncSession) -> None:
    sm, settings, run_id = await _polling(db_session)

    async def agent(work: runs.StepWork) -> engine.Outcome:
        return engine.Succeeded({"n": 3})

    client = _Rows(crash=True)
    loops = WorkflowEngine(
        sm, settings, {"agent": agent}, owner="w1", clock=lambda: NOW, poll_client=client
    )
    await polls_step(loops)  # crashes inside its step boundary
    await run_once(loops)
    async with sm() as session:
        steps = {s.node_id: s.status for s in await workflow_runs.list_steps(session, run_id)}
    assert client.calls == 1, "the bad poll ran"
    assert steps["a"] == "succeeded", "the loops went on past it"


async def test_a_run_whose_spec_no_longer_reads_fails_alone(db_session: AsyncSession) -> None:
    """A version published by a later release (a field this server does not know) fails its
    run with a plain reason; the loops go on and finish the other runs."""
    sm, settings, broken = await _started(db_session)
    _, _, healthy = await _started(db_session)
    await db_session.execute(
        text(
            "UPDATE workflow_versions SET spec = (spec::jsonb || '{\"later\": true}')::json"
            " WHERE workflow_id = (SELECT workflow_id FROM workflow_runs WHERE id = :run)"
        ),
        {"run": broken},
    )
    await db_session.commit()

    async def agent(work: runs.StepWork) -> engine.Outcome:
        return engine.Succeeded({"n": 3})

    await run_once(WorkflowEngine(sm, settings, {"agent": agent}, owner="w1", clock=lambda: NOW))
    async with sm() as session:
        failed = await workflow_runs.get_run(session, broken)
        done = await workflow_runs.get_run(session, healthy)
        steps = await workflow_runs.list_steps(session, broken)
        events = await list_events(session, broken)
    assert failed is not None and failed.status == "failed", failed
    assert failed.failure_reason == "engine_error", "the run says why it stopped"
    assert all(s.status == "cancelled" for s in steps), "its steps end with it"
    assert verify(broken, events).ok, "its ledger still verifies"
    assert done is not None and done.status == "succeeded", "the other run finished"
