"""The conversational working-repo tool checks target identity and manager rights."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import github_working_repo as working_tool
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
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
