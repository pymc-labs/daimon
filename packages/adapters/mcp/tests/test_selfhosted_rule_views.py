"""Neutral MCP reads retain isolation without accessing an Anthropic client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.middleware.mcp_identity import IdentityMiddleware
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.here import register_here_tools
from daimon.adapters.mcp.tools.slack._models import SlackChannelRow
from daimon.adapters.mcp.tools.tenant_summary import register_tenant_summary_tools
from daimon.adapters.mcp.tools.timers import register_timer_tools
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.config import DatabaseSettings, Settings
from daimon.core.continuity.timers import schedule_timer
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.selfhosted_resources import SelfHostedAgents, list_live_agent_facts
from daimon.testing.factories import make_account, make_tenant
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec
from pydantic import PostgresDsn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class NoAnthropic:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"neutral tool accessed Anthropic.{name}")


@pytest.mark.parametrize("identity", ["live", "unknown", "foreign", "archived"])
async def test_real_neutral_tools_resolve_local_isolation(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    foreign = await make_tenant(db_session, platform="slack")
    account = await make_account(db_session, tenant=tenant)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            channel_rules={
                "C_HOME": ChannelRule(readers="own", writers="own"),
                "C_SECRET": ChannelRule(readers="own", writers="own"),
            },
            agent_rules={
                "local": AgentRule(runs_in=("C_HOME",)),
                "secret": AgentRule(runs_in=("C_SECRET",)),
            },
        ),
    )
    for channel, name in (("C_HOME", "local"), ("C_SECRET", "secret")):
        await set_fields(
            db_session,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=channel),
            tenant_id=tenant.id,
            agent_name=name,
        )
    await db_session.commit()
    agents = SelfHostedAgents(sessionmaker)
    scope = Scope(
        tenant_id=str(tenant.id),
        account_id=str(account.id),
        principal_id="test",
        authorization_id="test",
    )
    local = await agents.create(
        scope,
        AgentSpec(name="local", model=ModelRef(provider="selfhosted", id="gpt-5")),
        key="local",
    )
    await agents.create(
        scope,
        AgentSpec(name="secret", model=ModelRef(provider="selfhosted", id="gpt-5")),
        key="secret",
    )
    executing = local.ref.id
    if identity == "foreign":
        foreign_agent = await agents.create(
            Scope(
                tenant_id=str(foreign.id),
                account_id=str(account.id),
                principal_id="test",
                authorization_id="test",
            ),
            AgentSpec(name="foreign", model=ModelRef(provider="selfhosted", id="gpt-5")),
            key="foreign",
        )
        executing = foreign_agent.ref.id
    elif identity == "archived":
        await agents.archive(scope, local.ref, key="archive")
    elif identity == "unknown":
        executing = "sh_agent_unknown"
    async with sessionmaker() as session:
        facts = await list_live_agent_facts(session, tenant_id=tenant.id)
    assert {a.name for a in facts} == (
        {"secret"} if identity == "archived" else {"local", "secret"}
    )
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=cast(AsyncAnthropic, NoAnthropic()),
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            _env_file=None,  # type: ignore[call-arg]
        ),
        deployment_default=DeploymentDefault(),
    )
    token = AccessToken(
        token="t",
        client_id="c",
        scopes=[],
        claims={
            "anthropic_disabled": True,
            "platform": "slack",
            "external_id": "T_TEST",
            "platform_user_id": "U_CALLER",
            "chat_agent_id": str(derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=executing)),
        },
    )
    monkeypatch.setattr(
        "daimon.adapters.mcp.middleware.mcp_identity.get_access_token", lambda: token
    )
    monkeypatch.setattr(
        "daimon.adapters.mcp.tools.here._slack_list_channels_impl",
        AsyncMock(
            return_value=[
                SlackChannelRow(id="C_HOME", name="home", type="public_channel", is_private=False),
                SlackChannelRow(
                    id="C_SECRET", name="secret", type="private_channel", is_private=True
                ),
            ]
        ),
    )

    async def subject(_ctx: object) -> str:
        return str(account.id)

    async def tenant_resolver(_ctx: object) -> str:
        return str(tenant.id)

    async def role(_ctx: object) -> str:
        return "admin"

    async def no_agent(_ctx: object) -> str | None:
        return None

    async def yes(_ctx: object) -> str:
        return "true"

    async def no(_ctx: object) -> str:
        return "false"

    mcp = FastMCP(name="neutral-local-reads")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=subject,
            tenant_resolver=tenant_resolver,
            role_resolver=role,
            agent_id_resolver=no_agent,
            is_admin_resolver=yes,
            internal_resolver=no,
            sessionmaker=sessionmaker,
        )
    )
    register_timer_tools(mcp, runtime)
    register_here_tools(mcp, runtime)
    register_tenant_summary_tools(mcp, runtime)
    for channel, name in (("C_HOME", "local"), ("C_SECRET", "secret")):
        await schedule_timer(
            sessionmaker,
            tenant_id=tenant.id,
            platform="slack",
            parent_channel_id=channel,
            thread_id=f"thread-{channel}",
            requester_account_id=account.id,
            requester_external_user_id="U_CALLER",
            target_ma_agent_id=local.ref.id,
            target_name=name,
            note=f"private note for {name}",
            fire_at=datetime.now(UTC) + timedelta(hours=2),
        )
    async with Client(mcp) as client:
        if identity != "live":
            for tool, args in (
                ("list_timers", {}),
                ("where_am_i", {"channel_id": "C_HOME"}),
                ("get_tenant_summary", {}),
            ):
                with pytest.raises(ToolError, match="agent could not be found"):
                    await client.call_tool(tool, args)
            return
        timers = await client.call_tool("list_timers", {})
        assert [timer.agent_name for timer in timers.data] == ["local"]
        assert [timer.note for timer in timers.data] == ["private note for local"]
        card = await client.call_tool("where_am_i", {"channel_id": "C_HOME"})
        assert card.data.agent_name == "local"
        assert card.data.agent_home == "C_HOME"
        with pytest.raises(ToolError, match="outside this conversation's home"):
            await client.call_tool("where_am_i", {"channel_id": "C_SECRET"})
        summary = await client.call_tool("get_tenant_summary", {})
        channels = {row.channel_id: row for row in summary.data.channels}
        assert channels["C_HOME"].agent_name == "local"
        assert channels["C_SECRET"].agent_name is None
