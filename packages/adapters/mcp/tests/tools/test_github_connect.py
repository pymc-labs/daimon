"""Agent-bound GitHub connect requests from a conversation."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import github_connect as connect_tool
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    GithubAppSettings,
    McpSettings,
    Settings,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_connect import digest, get_invitation
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_member_request_is_recorded_and_admin_link_goes_only_to_private_delivery(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, workspace_id="workspace")
        account = await make_account(session, tenant=tenant)
    runtime = McpRuntime(
        session_factory=committing_sessionmaker,
        client=AsyncMock(),  # type: ignore[arg-type]
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test")),
            anthropic=AnthropicSettings(api_key=SecretStr("test")),
            mcp=McpSettings(public_url=HttpUrl("https://mcp.test/mcp")),
            github_app=GithubAppSettings(
                app_id="42",
                app_slug="sample-app",
                private_key=SecretStr("pem"),
                client_id="client",
                client_secret=SecretStr("secret"),
            ),
        ),
        deployment_default=DeploymentDefault(),
    )
    origin = SimpleNamespace(
        configuration_target_name=None,
        configuration_target_ma_agent_id=None,
        responder_name="ResearchBot",
        responder_ma_agent_id="agent_research",
        parent_channel_id="channel",
    )
    agent = SimpleNamespace(id="agent_research", name="ResearchBot")
    delivery = AsyncMock()
    monkeypatch.setattr(connect_tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(connect_tool, "resolve_setup_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(connect_tool, "send_direct_message_impl", delivery)
    member = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        external_id="workspace",
        platform_user_id="123",
        is_admin=False,
    )
    result = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, member, origin_context_id=str(uuid.uuid4())
    )
    assert result.status == "ask_admin" and result.message == "Ask an admin"
    delivery.assert_not_awaited()
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent.id)
    async with committing_sessionmaker() as session:
        request = (
            await session.execute(
                text(
                    "SELECT requester_account_id FROM github_connect_requests "
                    "WHERE agent_id = :agent_id"
                ),
                {"agent_id": agent_id},
            )
        ).one_or_none()
        assert request is not None and request[0] == account.id
    async with committing_sessionmaker.begin() as session:
        await set_role(session, account.id, Role.ADMIN)
    admin = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.ADMIN,
        platform="discord",
        external_id="workspace",
        platform_user_id="123",
        is_admin=True,
    )
    result = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, admin, origin_context_id=str(uuid.uuid4())
    )
    assert result.status == "sent" and "http" not in result.message
    delivery.assert_awaited_once()
    content = delivery.await_args.kwargs["content"]
    assert content.startswith(
        "Connect GitHub for ResearchBot:\nhttps://mcp.test/oauth/github/connect/"
    )
    assert len(content.splitlines()) == 2
    assert delivery.await_args.kwargs["recipient_id"] == "123"
    delivery.side_effect = ToolError("DM blocked")
    blocked = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, admin, origin_context_id=str(uuid.uuid4())
    )
    assert blocked.status == "dm_blocked"
    assert blocked.message == "I can't DM you. Run /github connect here."
    blocked_url = delivery.await_args.kwargs["content"].splitlines()[1]
    async with committing_sessionmaker() as session:
        assert await get_invitation(session, digest(blocked_url.rsplit("/", 1)[1])) is None
    async with committing_sessionmaker.begin() as session:
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy.model_validate(
                {"agent_rules": {"ResearchBot": {"runs_in": ["client-channel"]}}}
            ),
        )
    delivery.reset_mock()
    pinned = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, admin, origin_context_id=str(uuid.uuid4())
    )
    assert pinned.status == "client_agent"
    assert pinned.message == (
        "This agent uses its saved GitHub key. Ask your Daimon operator to change it."
    )
    delivery.assert_not_awaited()
