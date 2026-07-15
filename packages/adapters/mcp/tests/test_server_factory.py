"""create_mcp_app factory: settings validation + health routes."""

from __future__ import annotations

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    Settings,
)
from daimon.core.errors import BootstrapError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


def _make_stub_anthropic() -> AsyncAnthropic:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    return AsyncAnthropic(
        api_key="sk-ant-test-stub",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
    )


_EXPECTED_TOOLS = {
    "list_agents",
    "get_agent",
    "create_agent",
    "update_agent",
    "fork_agent",
    "archive_agent",
    "list_environments",
    "get_environment",
    "create_environment",
    "update_environment",
    "archive_environment",
    "list_credentials",
    "list_channels",
    "list_threads",
    "read_thread",
    "read_channel",
    "parse_link",
    "get_message",
    "send_message",
    "search_messages",
}


def _settings(*, discord: DiscordSettings | None = None, **mcp: object) -> Settings:
    return Settings(
        database=DatabaseSettings(
            url=PostgresDsn("postgresql+asyncpg://u:p@h/d"),
        ),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(**mcp),  # pyright: ignore[reportArgumentType]
        discord=discord
        if discord is not None
        else DiscordSettings(bot_token=SecretStr("test-bot-token")),
    )


def test_factory_rejects_missing_jwt_secret() -> None:
    settings = _settings(public_url=HttpUrl("https://x/mcp"))
    with pytest.raises(BootstrapError, match="JWT_SECRET"):
        create_mcp_app(settings=settings)


def test_factory_rejects_missing_public_url() -> None:
    settings = _settings(jwt_secret=SecretStr("a" * 32))
    with pytest.raises(BootstrapError, match="PUBLIC_URL"):
        create_mcp_app(settings=settings)


def test_factory_rejects_short_jwt_secret() -> None:
    settings = _settings(
        jwt_secret=SecretStr("short"),
        public_url=HttpUrl("https://x/mcp"),
    )
    with pytest.raises(BootstrapError, match="32"):
        create_mcp_app(settings=settings)


def test_factory_accepts_valid_settings() -> None:
    settings = _settings(
        jwt_secret=SecretStr("a" * 32),
        public_url=HttpUrl("https://x/mcp"),
    )
    app = create_mcp_app(settings=settings)
    assert app is not None


async def test_create_mcp_app_registers_all_phase_2_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings(
        jwt_secret=SecretStr("a" * 32),
        public_url=HttpUrl("https://x/mcp"),
    )
    app = create_mcp_app(
        settings=settings,
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=_make_stub_anthropic(),
    )
    mcp = app.state.mcp
    registered = {t.name for t in await mcp.local_provider.list_tools()}
    assert registered >= _EXPECTED_TOOLS


def test_factory_boots_without_discord_settings() -> None:
    settings = _settings(
        jwt_secret=SecretStr("a" * 32),
        public_url=HttpUrl("https://x/mcp"),
    )
    settings.discord = None
    app = create_mcp_app(settings=settings)
    assert app is not None, "factory should succeed without discord settings"


async def test_factory_omits_discord_tools_when_no_discord_settings(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings(
        jwt_secret=SecretStr("a" * 32),
        public_url=HttpUrl("https://x/mcp"),
    )
    settings.discord = None
    app = create_mcp_app(
        settings=settings,
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=_make_stub_anthropic(),
    )
    mcp = app.state.mcp
    registered = {t.name for t in await mcp.local_provider.list_tools()}
    # read_channel and search_messages are discord-only; send_message also
    # exists in sessions tools so it is not a reliable discord discriminator.
    discord_only_tools = {"read_channel", "search_messages"}
    assert not (registered & discord_only_tools), (
        "discord tools should not be registered without bot token"
    )


def test_factory_has_no_stripe_route_when_billing_absent() -> None:
    from starlette.routing import Route

    settings = _settings(
        jwt_secret=SecretStr("a" * 32),
        public_url=HttpUrl("https://x/mcp"),
    )
    app = create_mcp_app(settings=settings, billing_config=None)
    route_paths = [r.path for r in app.routes if isinstance(r, Route)]
    assert "/webhooks/stripe" not in route_paths, "stripe route absent when billing unconfigured"
