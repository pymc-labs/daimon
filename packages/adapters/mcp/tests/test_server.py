"""End-to-end smoke test for sessions + time tool registration.

Sibling to ``test_server_factory.py``: that file covers settings validation
and the four pre-existing tool groups; this file covers the wave-2 addition
of the sessions and time tool groups.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    Settings,
)
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


def _settings() -> Settings:
    return Settings(
        database=DatabaseSettings(
            url=PostgresDsn("postgresql+asyncpg://u:p@h/d"),
        ),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(  # pyright: ignore[reportArgumentType]
            jwt_secret=SecretStr("a" * 32),
            public_url=HttpUrl("https://x/mcp"),
        ),
        discord=DiscordSettings(bot_token=SecretStr("test-bot-token")),
    )


async def test_create_mcp_app_registers_sessions_and_time_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_mcp_app(
        settings=_settings(),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=AsyncMock(),
    )
    mcp = app.state.mcp
    registered = {t.name for t in await mcp.local_provider.list_tools()}

    expected_new = {
        "list_sessions",
        "get_session",
        "list_session_events",
        "send_message",
        "now",
        "convert",
    }
    missing_new = expected_new - registered
    assert not missing_new, f"sessions+time tools must be registered; missing: {missing_new}"

    assert "list_agents" in registered, "agent tools should still be registered"
    assert "list_environments" in registered, (
        "environment tools should still be registered after sessions/time additions"
    )
    assert "list_credentials" in registered, "vault tools should still be registered"

    expected_routines = {
        "create_routine",
        "list_routines",
        "get_routine",
        "update_routine",
        "delete_routine",
    }
    missing_routines = expected_routines - registered
    assert not missing_routines, f"routines tools must be registered; missing: {missing_routines}"

    # all 7 self-edit tools wired into create_mcp_app.
    expected_self_edit = {
        "self_write_file",
        "self_read_file",
        "self_list_files",
        "self_delete_file",
        "set_repo_binding",
        "get_repo_binding",
        "clear_repo_binding",
    }
    missing_self_edit = expected_self_edit - registered
    assert not missing_self_edit, (
        f"self-edit tools must be registered; missing: {missing_self_edit}"
    )
