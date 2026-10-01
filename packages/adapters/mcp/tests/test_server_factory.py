"""create_mcp_app factory: settings validation + health routes."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    ArtifactsSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    Settings,
)
from daimon.core.errors import BootstrapError
from daimon.testing.ma import build_stub_anthropic
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


async def test_factory_builds_one_process_scoped_artifact_store() -> None:
    settings = _settings(
        jwt_secret=SecretStr("a" * 32),
        public_url=HttpUrl("https://x/mcp"),
    )
    settings.artifacts = ArtifactsSettings(
        endpoint_url="https://bucket.example.test",
        bucket="private-artifacts",
        access_key_id="access-key",
        secret_access_key="secret-key",
    )

    with patch("daimon.adapters.mcp.server.build_artifact_store") as build_store:
        app = create_mcp_app(settings=settings)

    assert app is not None
    build_store.assert_called_once_with(settings.artifacts)


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
        anthropic=build_stub_anthropic(),
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
        anthropic=build_stub_anthropic(),
    )
    mcp = app.state.mcp
    registered = {t.name for t in await mcp.local_provider.list_tools()}
    # read_channel and search_messages are discord-only.
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


class _WarningCollector(logging.Handler):
    """Collects log records emitted directly on the ``fastmcp`` logger.

    ``pytest``'s ``caplog`` fixture only ever attaches its capture handler to
    the *root* logger, but ``fastmcp`` disables propagation on its own
    top-level logger at import time (``fastmcp/utilities/logging.py``'s
    ``configure_logging``), so a duplicate-registration warning never reaches
    root and ``caplog`` cannot see it. Attaching this handler directly to the
    ``fastmcp`` logger observes it at the point ``fastmcp`` itself emits it.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


async def test_create_mcp_app_logs_no_duplicate_tool_registrations(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A second tool registered under a name that already exists silently
    replaces the first in local_provider.list_tools() — the only observable
    signal is a WARNING log record. Assembling the full app (Discord settings
    present, so the channel tools register too) must produce none, so a
    future collision between two tool groups fails this test instead of
    shipping as an unreachable tool.
    """
    settings = _settings(
        jwt_secret=SecretStr("a" * 32),
        public_url=HttpUrl("https://x/mcp"),
    )
    collector = _WarningCollector()
    fastmcp_logger = logging.getLogger("fastmcp")
    fastmcp_logger.addHandler(collector)
    try:
        create_mcp_app(
            settings=settings,
            sessionmaker=sessionmaker,
            auth=StaticTokenVerifier(tokens={}),
            anthropic=build_stub_anthropic(),
        )
    finally:
        fastmcp_logger.removeHandler(collector)
    duplicates = [r.getMessage() for r in collector.records if "already exists" in r.getMessage()]
    assert not duplicates, f"duplicate tool/component registrations found: {duplicates}"
