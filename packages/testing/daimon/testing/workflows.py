"""A workflow and its runs on the test database, for executor, loop and tool tests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from daimon.core._models import Tenant
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.stores import workflow_runs, workflows
from daimon.core.stores.domain import Platform
from daimon.core.stores.workflow_run_events import list_events
from daimon.core.workflows import engine, runs
from daimon.core.workflows.ledger import verify
from daimon.core.workflows.preflight import Refusal
from daimon.core.workflows.runs import Start, StepWork
from daimon.testing.factories import make_account, make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
Sessions = async_sessionmaker[AsyncSession]


def make_settings(**workflows_settings: Any) -> Settings:  # noqa: ANN401 - WorkflowSettings fields
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
            "features": {"workflows": "on"},
            "workflows": workflows_settings,
        }
    )


@dataclass
class World:
    sm: Sessions
    settings: Settings
    tenant_id: uuid.UUID
    owner: uuid.UUID
    workflow_id: uuid.UUID

    def start(self, **kw: Any) -> Start:  # noqa: ANN401 - forwards Start fields
        kw.setdefault("run_as_account_id", self.owner)
        kw.setdefault("triggered_by", {"kind": "person"})
        return Start(self.workflow_id, **kw)

    async def run(self, **kw: Any) -> workflow_runs.RunRow:  # noqa: ANN401 - Start fields
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

    async def items(self, run_id: uuid.UUID) -> dict[str, workflow_runs.StepRow]:
        """For each items by key, `each[0]` and on."""
        async with self.sm() as s:
            return {runs.key_of(r): r for r in await workflow_runs.list_items(s, run_id=run_id)}

    async def claim(self, run_id: uuid.UUID, key: str, now: datetime = NOW) -> runs.Claimed:
        step = ((await self.steps(run_id)) | (await self.items(run_id)))[key]
        claimed = await runs.claim(
            self.sm, step_id=step.id, owner="w1", lease=timedelta(minutes=5), now=now
        )
        assert claimed is not None, f"{key} is claimed"
        return claimed

    async def execute(
        self, claimed: runs.Claimed, *outcomes: engine.Outcome, now: datetime = NOW
    ) -> None:
        """Run the claimed step with a fake executor that answers `outcomes` in turn."""
        queue = list(outcomes)

        async def fake(work: StepWork) -> engine.Outcome:
            assert work.attempt == claimed.attempt, "the executor sees its attempt"
            return queue.pop(0)

        fakes: dict[str, runs.StepExecutor] = dict.fromkeys(
            ("agent", "action", "approval", "code"), fake
        )
        await runs.execute(self.sm, self.settings, fakes, claimed, clock=lambda: now)

    async def add_workflow(self, nodes: dict[str, Any], start: str) -> uuid.UUID:
        """Another published workflow of the owner's, with a call trigger."""
        spec = _spec(nodes, start) | {"triggers": [{"id": "call", "kind": "call"}]}
        async with self.sm.begin() as s:
            return await _publish(s, self.tenant_id, self.owner, spec, None)

    async def verified(self, run_id: uuid.UUID) -> list[str]:
        async with self.sm() as s:
            events = await list_events(s, run_id)
        run = await self.get(run_id)
        result = verify(run_id, events)
        assert result.ok and result.head == run.head_hash, f"the ledger verifies: {result}"
        return [e.type for e in events]


async def make_world(
    db_session: AsyncSession,
    nodes: dict[str, Any],
    start: str,
    *,
    platform: Platform = "discord",
    home: str | None = None,
    workspace_id: str | None = None,
    **settings: Any,  # noqa: ANN401 - WorkflowSettings fields
) -> World:
    tenant = await make_tenant(db_session, platform=platform, workspace_id=workspace_id)
    owner = await make_account(db_session, tenant=tenant)
    await db_session.execute(
        update(Tenant).where(Tenant.id == tenant.id).values(funding_mode="operator_funded")
    )
    spec = _spec(nodes, start) | ({"home": {"channel": home}} if home else {})
    flow_id = await _publish(db_session, tenant.id, owner.id, spec, home)
    await db_session.commit()
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    return World(sm, make_settings(**settings), tenant.id, owner.id, flow_id)


def _spec(nodes: dict[str, Any], start: str) -> dict[str, Any]:
    return {"schema": "daimon.workflow/v1", "name": "T", "start": start, "nodes": nodes}


async def _publish(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    owner: uuid.UUID,
    spec: dict[str, Any],
    home: str | None,
) -> uuid.UUID:
    flow = await workflows.create_workflow(
        session,
        tenant_id=tenant_id,
        owner_account_id=owner,
        name=spec["name"],
        spec=spec,
        home_channel_id=home,
    )
    await workflows.publish_version(
        session,
        tenant_id=tenant_id,
        workflow_id=flow.id,
        spec=spec,
        spec_hash="h",
        source="web",
        published_by=owner,
    )
    return flow.id
