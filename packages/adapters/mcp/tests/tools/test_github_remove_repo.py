"""Conversational removal acts immediately and is scoped to one agent."""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import github_remove_repo as tool
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import github_app_installations
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_owner_grants import resolve_owner_repositories
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_mixed_case_owner_repo_removal_saves_canonical_exclusion(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, workspace_id=f"remove-owner-{uuid.uuid4().hex[:8]}")
        admin = await make_account(session, tenant=tenant)
        await set_role(session, admin.id, Role.ADMIN)
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ma_agent")
        await github_app_installations.upsert(
            session, installation_id=98765, account_login="owner", repo_full_names=["owner/repo"]
        )
        await session.execute(
            text(
                "INSERT INTO agent_github_owner_grants "
                "(tenant_id, agent_id, installation_id, owner_login, access) "
                "VALUES (:tenant, :agent, 98765, 'owner', 'read')"
            ),
            {"tenant": tenant.id, "agent": agent_id},
        )
    origin = SimpleNamespace(
        configuration_target_name="Agent",
        configuration_target_ma_agent_id="ma_agent",
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="channel",
        thread_id="thread",
    )
    monkeypatch.setattr(tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(
        tool,
        "resolve_setup_agent",
        AsyncMock(return_value=SimpleNamespace(id="ma_agent", name="Agent", metadata={})),
    )
    runtime = SimpleNamespace(
        session_factory=committing_sessionmaker,
        deployment_default=DeploymentDefault(),
        group_lookups=None,
    )
    auth = AuthIdentity(
        account_id=admin.id,
        tenant_id=tenant.id,
        role=Role.ADMIN,
        platform="discord",
        external_id="workspace",
        platform_user_id="admin",
        is_admin=True,
    )
    result = await tool.remove_repo_impl(
        runtime,  # type: ignore[arg-type]
        auth,
        origin_context_id="synthetic",
        repo_name="Owner/Repo",
    )
    assert result.status == "removed"
    assert result.message == "Agent can no longer use Owner/Repo.\nThe other Owner repos stay."
    assert "http" not in result.message
    async with committing_sessionmaker() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT repo_full_name FROM agent_github_owner_exclusions "
                    "WHERE tenant_id = :tenant AND agent_id = :agent"
                ),
                {"tenant": tenant.id, "agent": agent_id},
            )
            == "owner/repo"
        )
        assert not await resolve_owner_repositories(session, tenant_id=tenant.id, agent_id=agent_id)


@pytest.mark.asyncio
async def test_teams_admin_reaches_remove_repo_and_external_caller_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teams passes the platform gate to the agent's list; a guest is still refused."""
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
    )
    with pytest.raises(ToolError, match="not on this agent's list"):
        await tool.remove_repo_impl(
            runtime,  # type: ignore[arg-type]
            admin,
            origin_context_id=str(uuid.uuid4()),
            repo_name="owner/missing",
        )
    with pytest.raises(ToolError, match="member of this Discord, Slack or Teams workspace"):
        await tool.remove_repo_impl(
            runtime,  # type: ignore[arg-type]
            replace(admin, is_external=True),
            origin_context_id=str(uuid.uuid4()),
            repo_name="owner/missing",
        )
