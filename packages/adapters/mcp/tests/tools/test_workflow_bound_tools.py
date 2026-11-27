"""A workflow run's step grant (the lead-intake seam) survives the vault and signs its run,
in place of a routine's, into the MCP credential."""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.core.config import DiscordSettings
from daimon.core.discord_membership import DiscordRestMembershipResolver
from daimon.core.lead_intake_execution import (
    WorkflowExecution,
    current_intake_execution,
    execution_from_metadata,
    execution_metadata,
)
from daimon.core.mcp_auth import mint_chat_mcp_token
from daimon.core.membership import Membership
from daimon.core.stores import identity
from daimon.core.stores.lead_intake import workflow_execution
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.workflows import World, make_world
from fastmcp import Context, FastMCP
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from ..harness import production_identity_middleware

OWNER = "111111111111111111"
SECRET = b"chat-integration-secret-at-least-32-bytes"
STEP: dict[str, Any] = {
    "r": {"kind": "agent", "agent": "helper", "instructions": "Triage the inbox", "next": "f"},
    "f": {"kind": "finish"},
}


async def _world(db_session: AsyncSession) -> World:
    world = await make_world(db_session, STEP, "r", home="555", workspace_id="999999999999999999")
    async with world.sm.begin() as s:
        await identity.create_platform_principal(
            s, tenant_id=world.tenant_id, platform="discord", external_id=OWNER,
            account_id=world.owner,
        )  # fmt: skip
    return world


def test_a_workflow_grant_survives_the_vault() -> None:
    grant = WorkflowExecution(uuid.uuid4(), uuid.uuid4())
    back = execution_from_metadata(execution_metadata(grant))
    assert isinstance(back, WorkflowExecution) and back == grant, "a refresh keeps the run"
    plain = (uuid.uuid4(), uuid.uuid4())
    assert type(execution_from_metadata(execution_metadata(plain))) is tuple, "a routine's too"


async def test_a_step_credential_signs_its_run_and_grant(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session)
    run = await world.run()

    async def member(*_a: object, **_kw: object) -> Membership:
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
    async def whoami(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> bool:
        seen["auth"] = await ctx.get_state("auth")
        return True

    result = await call_mcp_tool(mcp.http_app(), token=token, name="whoami")
    assert "error" not in result, result
    auth = seen["auth"]
    assert (auth.workflow_run_id, auth.execution_id, auth.routine_id) == (run.id, grant, None)
    assert auth.fire_bound and auth.platform_user_id == OWNER
