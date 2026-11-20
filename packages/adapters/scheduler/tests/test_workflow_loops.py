"""The workflow loops run a manual start to the end, through a fake agent executor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from daimon.adapters.scheduler.workflows import WorkflowEngine, run_once
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.stores import tenants, workflow_runs, workflows
from daimon.core.stores.workflow_run_events import list_events
from daimon.core.workflows import engine, runs
from daimon.core.workflows.ledger import verify
from daimon.core.workflows.preflight import Refusal
from daimon.testing.factories import make_account, make_tenant
from pydantic import PostgresDsn, SecretStr
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


async def test_a_manual_run_finishes_through_the_loops(db_session: AsyncSession) -> None:
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

    async def agent(work: runs.StepWork) -> engine.Outcome:
        return engine.Succeeded({"n": 3})

    await run_once(WorkflowEngine(sm, settings, {"agent": agent}, owner="w1", clock=lambda: NOW))

    async with sm() as session:
        done = await workflow_runs.get_run(session, run.id)
        steps = await workflow_runs.list_steps(session, run.id)
        events = await list_events(session, run.id)
    assert done is not None and (done.status, done.outcome) == ("succeeded", "completed"), done
    assert {s.node_id: s.output for s in steps}["f"] == {"double": 6}, "Finish renders its output"
    checked = verify(run.id, events)
    assert checked.ok and checked.head == done.head_hash, f"the ledger verifies: {checked}"
