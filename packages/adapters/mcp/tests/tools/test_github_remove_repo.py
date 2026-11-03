"""Conversational removal acts immediately and is scoped to one agent."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import github_remove_repo as tool
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_teams_admin_reaches_remove_repo(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teams passes the platform gate to the agent's list."""
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, workspace_id=f"teams-remove-{uuid.uuid4().hex[:8]}")
        account = await make_account(session, tenant=tenant)
        await set_role(session, account.id, Role.ADMIN)
    origin = SimpleNamespace(
        configuration_target_name="Agent",
        configuration_target_ma_agent_id="ma_agent",
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="19:c@thread.tacv2",
    )
    runtime = SimpleNamespace(
        session_factory=committing_sessionmaker,
        deployment_default=DeploymentDefault(),
        group_lookups=None,
    )
    monkeypatch.setattr(tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(
        tool,
        "resolve_setup_agent",
        AsyncMock(return_value=SimpleNamespace(id="ma_agent", name="Agent", metadata={})),
    )
    admin = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.ADMIN,
        platform="teams",
        external_id="entra-tenant",
        platform_user_id="aad-admin",
        is_admin=True,
        is_platform_admin=True,
    )
    with pytest.raises(ToolError, match="not on this agent's list"):
        await tool.remove_repo_impl(
            runtime,  # type: ignore[arg-type]
            admin,
            origin_context_id=str(uuid.uuid4()),
            repo_name="owner/missing",
        )
