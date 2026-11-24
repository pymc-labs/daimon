"""A workflow's routine step binds its tools as a routine fire does: its run's live grant, its
own tenant and owner, nothing more."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.adapters.mcp.identity_relay import (
    _workflow_step_run,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import routine_run
from daimon.adapters.mcp.tools.lead_intake import intake_fire
from daimon.core._models import OpsEvent, PlatformPrincipal
from daimon.core.config import DiscordSettings
from daimon.core.discord_membership import DiscordRestMembershipResolver
from daimon.core.lead_intake import IntakeConfig
from daimon.core.lead_intake_execution import (
    WorkflowExecution,
    current_intake_execution,
    execution_from_metadata,
    execution_metadata,
)
from daimon.core.mcp_auth import mint_chat_mcp_token
from daimon.core.mcp_identity import RelayBinding, parse_relay_claim
from daimon.core.membership import Membership
from daimon.core.ops_events import drain_ops_events
from daimon.core.stores.lead_intake import workflow_execution
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.workflows import World, make_world
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..harness import production_identity_middleware
from .parity_fakes import make_auth

OWNER = "111111111111111111"
SECRET = b"chat-integration-secret-at-least-32-bytes"
STEP: dict[str, Any] = {
    "r": {
        "kind": "routine",
        "agent": "helper",
        "prompt": "Triage the inbox",
        "to": {"kind": "channel", "id": "555"},
        "next": "f",
    },
    "f": {"kind": "finish"},
}


async def _world(db_session: AsyncSession, workspace: str = "999999999999999999") -> World:
    world = await make_world(
        db_session,
        STEP,
        "r",
        home="555",
        workspace_id=workspace,
        spec_fields={"concurrency": {"limit": 1, "on_conflict": "skip"}},
    )
    async with world.sm.begin() as s:
        s.add(
            PlatformPrincipal(
                tenant_id=world.tenant_id,
                platform="discord",
                external_id=OWNER,
                account_id=world.owner,
            )
        )
    return world


def _config(workflow_id: uuid.UUID) -> IntakeConfig:
    return IntakeConfig(
        agent_id=uuid.uuid4(),
        guild_id="1",
        channel_id="2",
        source_connection_id="gmail-1",
        crm_connection_id="crm-1",
        portal_id="3",
        workflow_id=workflow_id,
        reconciliation_start=datetime.now(UTC) - timedelta(days=1),
    )


def test_a_workflow_grant_survives_the_vault_and_the_relay_claim() -> None:
    grant = WorkflowExecution(uuid.uuid4(), uuid.uuid4())
    back = execution_from_metadata(execution_metadata(grant))
    assert isinstance(back, WorkflowExecution) and back == grant, "a refresh keeps the run"
    plain = (uuid.uuid4(), uuid.uuid4())
    assert type(execution_from_metadata(execution_metadata(plain))) is tuple, "a routine's too"
    relay = RelayBinding(agent_name="helper", workflow_run_id=grant.run_id, owner=OWNER)
    assert parse_relay_claim(relay.claim()) == relay
    with pytest.raises(ValueError, match="wrong shape"):
        parse_relay_claim(relay.claim() | {"routine": str(uuid.uuid4())})


async def test_a_step_credential_signs_its_run_and_grant(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session)
    run = await world.run()

    async def member(self, tenant_id, guild_id, user_id, require_fresh=False):
        return Membership(is_member=True, is_admin=False)

    monkeypatch.setattr(DiscordRestMembershipResolver, "resolve", member)
    async with workflow_execution(world.sm, world.tenant_id, run.id, lifetime_s=60) as grant:
        held = current_intake_execution.get()
        async with world.sm.begin() as s:
            token, _, _ = await mint_chat_mcp_token(
                s,
                account_id=world.owner,
                agent_id=uuid.uuid4(),
                secret=SECRET,
                now=datetime.now(UTC),
                chat_session_id="cs1",
                routine_execution=held,
            )
    settings = MagicMock(discord=DiscordSettings(bot_token=SecretStr("test")))
    mcp = FastMCP(name="t", auth=DaimonJWTVerifier(secret=SECRET, sessionmaker=world.sm))
    mcp.add_middleware(production_identity_middleware(world.sm, settings=settings))
    seen: dict[str, Any] = {}

    @mcp.tool
    async def whoami(ctx: Context) -> bool:
        seen["auth"] = await ctx.get_state("auth")
        return True

    result = await call_mcp_tool(mcp.http_app(), token=token, name="whoami")
    assert "error" not in result, result
    auth = seen["auth"]
    assert (auth.workflow_run_id, auth.execution_id, auth.routine_id) == (run.id, grant, None)
    assert auth.fire_bound and auth.platform_user_id == OWNER


async def test_intake_and_problem_report_bind_the_step_and_refuse_another_run_or_tenant(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    other = await _world(db_session, "888888888888888888")
    run, foreign = await world.run(), await other.run()
    runtime = cast(McpRuntime, SimpleNamespace(session_factory=world.sm))
    config = _config(world.workflow_id)
    async with (
        workflow_execution(world.sm, world.tenant_id, run.id, lifetime_s=60) as grant,
        workflow_execution(other.sm, other.tenant_id, foreign.id, lifetime_s=60) as stray,
    ):
        auth = replace(
            make_auth(admin=False),
            platform_user_id=OWNER,
            tenant_id=world.tenant_id,
            workflow_run_id=run.id,
            execution_id=grant,
        )
        row, active = await intake_fire(runtime, auth, config)
        assert active and row is not None and row.id == world.workflow_id, "its own step"
        assert (await intake_fire(runtime, auth, _config(other.workflow_id)))[0] is None
        stranger = replace(auth, platform_user_id="222")
        assert (await intake_fire(runtime, stranger, config))[0] is None, "the owner only"
        wrong_run = replace(auth, execution_id=stray)
        cross = replace(auth, tenant_id=other.tenant_id, execution_id=stray)
        for refused in (wrong_run, cross):
            assert not (await intake_fire(runtime, refused, config))[1], "another run's grant"
            with pytest.raises(ToolError):
                await routine_run._report_routine_problem_impl(runtime, refused, "other", "x")
        out = await routine_run._report_routine_problem_impl(
            runtime, auth, "blocked_access", "The inbox is not shared."
        )
    assert (out.routine_id, out.workflow_id) == (None, str(world.workflow_id))
    await drain_ops_events()
    async with world.sm() as s:
        rows = (
            await s.execute(select(OpsEvent.code, OpsEvent.workflow_run_id, OpsEvent.note))
        ).all()
    assert [tuple(r) for r in rows] == [
        ("routine.goal_unmet", run.id, "The inbox is not shared.")
    ], "one event, for its own run only"
    with pytest.raises(ToolError):
        await routine_run._report_routine_problem_impl(runtime, auth, "other", "late")


async def test_the_relay_names_the_step_owner_only_for_its_own_run_agent_and_tenant(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    run = await world.run()
    signed = RelayBinding(agent_name="helper", workflow_run_id=run.id, owner=OWNER)
    step = await _workflow_step_run(world.sm, signed, tenant_id=world.tenant_id)
    assert step is not None and step.created_by_user_id == OWNER
    for refused, tenant in (
        (replace(signed, owner="222"), world.tenant_id),
        (replace(signed, agent_name="other"), world.tenant_id),
        (replace(signed, workflow_run_id=uuid.uuid4()), world.tenant_id),
        (signed, uuid.uuid4()),
    ):
        assert await _workflow_step_run(world.sm, refused, tenant_id=tenant) is None
