"""A chat agent that enables daimon tools by name sees each of them listed.

Managed Agents only exposes the tool names a toolset enables. Most daimon tools
are listed only behind `search_tools` and `call_tool`, so an agent whose list
names them directly lost every tool that was not also pinned beside the proxy.
"""

from __future__ import annotations

import datetime as dt
import uuid

from anthropic import AsyncAnthropic
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    Settings,
    TurnSettings,
)
from daimon.core.mcp_auth import mint_chat_mcp_token
from daimon.core.stores.agent_daimon_tools import record_enabled_daimon_tools
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.testing.asgi import mcp_session
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp

SECRET = "a" * 32

_WATCH_TOOLS = frozenset(
    {
        "create_watch",
        "resume_watch",
        "rotate_watch_key",
        "update_watch",
        "read_channel",
        "read_thread",
        "preview_watch_edit",
        "list_watches",
        "show_watch",
        "pause_watch",
        "delete_watch",
    }
)


def _make_app(sessionmaker: async_sessionmaker[AsyncSession], anthropic: AsyncAnthropic) -> ASGIApp:
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            # Channel tools register only on a deployment with a chat platform.
            discord=DiscordSettings(bot_token=SecretStr("discord-test-token")),
            turn=TurnSettings(channel_backends=False, agent_backends=False),
            mcp=McpSettings(
                legacy_token_cutoff=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
                allow_legacy_tokens=True,
                jwt_secret=SecretStr(SECRET),
                public_url=HttpUrl("https://x/mcp"),
            ),
        ),
        sessionmaker=sessionmaker,
        anthropic=anthropic,
    )


async def _chat_agent_token(
    sessionmaker: async_sessionmaker[AsyncSession], *, enabled: frozenset[str] | None
) -> str:
    """A Discord chat-turn credential for a fresh agent, its daimon list recorded."""
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="allowlisted-agent")
        account = await make_account(s, tenant=tenant)
        await make_platform_principal(
            s, platform="discord", external_id="discord-user-1", tenant=tenant, account=account
        )
    agent_id = uuid.uuid4()
    toolset = {
        "type": "mcp_toolset",
        "mcp_server_name": DAIMON_SERVER_NAME,
        "default_config": {"enabled": enabled is None},
        "configs": [{"name": name, "enabled": True} for name in sorted(enabled or ())],
    }
    await record_enabled_daimon_tools(
        sessionmaker, tenant_id=tenant.id, agent_id=agent_id, tools=[toolset]
    )
    async with sessionmaker() as session, session.begin():
        token, _, _ = await mint_chat_mcp_token(
            session,
            account_id=account.id,
            agent_id=agent_id,
            secret=SECRET.encode(),
            now=dt.datetime.now(dt.UTC),
        )
    return token


async def _listed(sessionmaker: async_sessionmaker[AsyncSession], token: str) -> set[str]:
    app = _make_app(sessionmaker, build_fake_anthropic(MARouter().dispatch))
    response = await mcp_session(app, token=token, method="tools/list")
    result = response["result"]
    assert isinstance(result, dict)
    return {tool["name"] for tool in result["tools"]}  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]


async def test_an_allowlisting_chat_agent_lists_every_tool_it_enables(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token = await _chat_agent_token(sessionmaker, enabled=_WATCH_TOOLS)
    assert await _listed(sessionmaker, token) == set(_WATCH_TOOLS), (
        "every tool the agent enables must be listed under its own name"
    )


async def test_a_chat_agent_without_a_list_keeps_the_search_proxy(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    listed = await _listed(sessionmaker, await _chat_agent_token(sessionmaker, enabled=None))
    assert {"search_tools", "call_tool"} <= listed
    assert "list_watches" not in listed, "an agent with no list keeps the search collapse"
