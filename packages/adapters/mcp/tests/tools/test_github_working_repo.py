"""The conversational working-repo tool checks target identity and manager rights."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import github_working_repo as working_tool
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import github_access, github_app_installations
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_set_working_repo_admin_none_and_member_refusal(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, workspace_id=f"working-{uuid.uuid4().hex[:8]}")
        admin_account = await make_account(session, tenant=tenant)
        member_account = await make_account(session, tenant=tenant)
        await set_role(session, admin_account.id, Role.ADMIN)
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    origin = SimpleNamespace(
        configuration_target_name="Agent",
        configuration_target_ma_agent_id="ma_agent",
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="channel",
    )
    runtime = SimpleNamespace(
        session_factory=committing_sessionmaker,
        deployment_default=DeploymentDefault(),
        group_lookups=None,
    )
    monkeypatch.setattr(working_tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(working_tool, "resolve_setup_agent", AsyncMock(return_value=agent))
    admin = AuthIdentity(
        account_id=admin_account.id,
        tenant_id=tenant.id,
        role=Role.ADMIN,
        platform="discord",
        external_id="workspace",
        platform_user_id="admin",
        is_admin=True,
    )
    result = await working_tool.set_working_repo_impl(
        runtime,  # type: ignore[arg-type]
        admin,
        origin_context_id=str(uuid.uuid4()),
        repo_name="none",
    )
    assert result.working_repo is None
    assert result.message == "Agent has no working repo."
    with pytest.raises(ToolError, match="Connect GitHub"):
        await working_tool.set_working_repo_impl(
            runtime,  # type: ignore[arg-type]
            admin,
            origin_context_id=str(uuid.uuid4()),
            repo_name="owner/missing",
        )
    member = AuthIdentity(
        account_id=member_account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        external_id="workspace",
        platform_user_id="member",
        is_admin=False,
    )
    with pytest.raises(ToolError, match="Ask a server admin"):
        await working_tool.set_working_repo_impl(
            runtime,  # type: ignore[arg-type]
            member,
            origin_context_id=str(uuid.uuid4()),
            repo_name="none",
        )
    manage_check = AsyncMock(return_value=True)
    monkeypatch.setattr(working_tool, "requester_manages_agent", manage_check)
    allowed = await working_tool.set_working_repo_impl(
        runtime,  # type: ignore[arg-type]
        member,
        origin_context_id=str(uuid.uuid4()),
        repo_name="none",
    )
    assert allowed.working_repo is None
    manage_check.assert_awaited_once()
    monkeypatch.setattr(working_tool, "set_working_repo", AsyncMock(return_value="owner/repo"))
    monkeypatch.setattr(
        working_tool,
        "list_agent_repos",
        AsyncMock(return_value=[SimpleNamespace(full_name="owner/repo", staged=True)]),
    )
    pending = await working_tool.set_working_repo_impl(
        runtime,  # type: ignore[arg-type]
        admin,
        origin_context_id=str(uuid.uuid4()),
        repo_name="owner/repo",
    )
    assert pending.pending
    assert pending.message == (
        "Agent will use owner/repo as its working repo when GitHub setup finishes."
    )


@pytest.mark.asyncio
async def test_working_repo_real_manager_and_foreign_scope(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, workspace_id=f"working-scope-{uuid.uuid4().hex[:8]}")
        account = await make_account(session, tenant=tenant)
        await set_channel_admins(
            session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="mine",
            role_ids=[],
            user_ids=["777"],
            actor_account_id=None,
        )
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy.model_validate(
                {"agent_rules": {"Agent": {"runs_in": ["theirs"]}}}
            ),
        )
        await github_app_installations.upsert(
            session,
            installation_id=98766,
            account_login="owner",
            repo_full_names=["owner/foreign"],
        )
        other_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ma_other")
        await session.execute(
            text(
                "INSERT INTO tenant_github_repos "
                "(tenant_id, repo_id, scope_agent_id, owner_id, installation_id, "
                "repo_full_name, max_access, authorized_by_github_user_id, "
                "authorized_by_account_id) VALUES "
                "(:tenant, 98766, :agent, 12, 98766, 'owner/foreign', 'read', 17, :account)"
            ),
            {"tenant": tenant.id, "agent": other_id, "account": account.id},
        )
        await github_access.stage_grant(
            session,
            tenant_id=tenant.id,
            agent_id=other_id,
            repo_id=98766,
            baseline_access="read",
            ceiling_access="read",
            granted_by_account_id=account.id,
        )
    runtime = SimpleNamespace(
        session_factory=committing_sessionmaker,
        deployment_default=DeploymentDefault(),
        group_lookups=None,
    )
    origin = SimpleNamespace(
        configuration_target_name="Agent",
        configuration_target_ma_agent_id="ma_agent",
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="mine",
    )
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(working_tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(working_tool, "resolve_setup_agent", AsyncMock(return_value=agent))
    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        external_id="workspace",
        platform_user_id="777",
    )
    with pytest.raises(ToolError, match="Ask a server admin"):
        await working_tool.set_working_repo_impl(
            runtime,
            auth,
            origin_context_id=str(uuid.uuid4()),
            repo_name="none",  # type: ignore[arg-type]
        )
    async with committing_sessionmaker.begin() as session:
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy.model_validate(
                {"agent_rules": {"Agent": {"runs_in": ["mine"]}}}
            ),
        )
    agent.metadata = {"daimon_managed": "true"}
    with pytest.raises(ToolError, match="Ask a server admin"):
        await working_tool.set_working_repo_impl(
            runtime,
            auth,
            origin_context_id=str(uuid.uuid4()),
            repo_name="none",  # type: ignore[arg-type]
        )
    agent.metadata = {}
    with pytest.raises(ToolError, match="Connect GitHub"):
        await working_tool.set_working_repo_impl(
            runtime,
            auth,
            origin_context_id=str(uuid.uuid4()),
            repo_name="owner/foreign",  # type: ignore[arg-type]
        )
