"""Organic participation runs on Discord and Teams; Slack only observes setup lifecycle.

Discord and Teams share one gate shell (`daimon.core.participation_gates`):
the same cascade, hourly cap, billing gates, classifier and batch bounds, and
the tool accepts callers from both. Slack message subscriptions track deletion
of setup roots. They do not grant permission to respond to unmentioned
messages; the participation tool keeps refusing Slack callers before any
database operation.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import yaml
from anthropic import AsyncAnthropic
from daimon.adapters.discord import bot as discord_bot
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.thread_participation import (
    _set_thread_participation_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.teams import participation as teams_participation
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    Settings,
)
from daimon.core.participation_gates import BATCH_MAX_MESSAGES, BATCH_MAX_QUIET_PERIODS
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr

_MANIFEST = Path(__file__).resolve().parents[2] / "docs" / "slack-app-manifest.yaml"


def _slack_bot_events() -> list[str]:
    manifest = cast(dict[str, Any], yaml.safe_load(_MANIFEST.read_text()))
    return cast(list[str], manifest["settings"]["event_subscriptions"]["bot_events"])


def test_slack_manifest_receives_setup_lifecycle_events() -> None:
    events = _slack_bot_events()
    assert {
        "message.channels",
        "message.groups",
        "channel_archive",
        "channel_unarchive",
        "channel_deleted",
    }.issubset(events), "setup lifecycle requires root deletion and channel events"


async def test_the_tool_refuses_every_platform_but_discord_and_teams() -> None:
    """A Slack caller is refused before the tool reads or writes anything."""
    runtime = McpRuntime(
        session_factory=MagicMock(side_effect=AssertionError("no DB read may happen")),
        client=MagicMock(spec=AsyncAnthropic),
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
            discord=DiscordSettings(bot_token=SecretStr("test-bot-token")),
        ),
        deployment_default=DeploymentDefault(),
    )
    slack_caller = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.ADMIN,
        platform="slack",
        is_admin=True,
    )

    with pytest.raises(ToolError) as err:
        await _set_thread_participation_impl(runtime, slack_caller, "on", "thread-1", None)

    assert "Discord and Teams" in str(err.value), (
        "set_thread_participation must keep refusing Slack callers until the Slack "
        "adapter reads thread_participation_scopes"
    )


async def test_a_teams_caller_passes_the_platform_check() -> None:
    """Teams follows threads: its caller is refused only for the missing Teams block."""
    runtime = McpRuntime(
        session_factory=MagicMock(side_effect=AssertionError("no DB read may happen")),
        client=MagicMock(spec=AsyncAnthropic),
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
            discord=DiscordSettings(bot_token=SecretStr("test-bot-token")),
        ),
        deployment_default=DeploymentDefault(),
    )
    teams_caller = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.USER, platform="teams"
    )

    with pytest.raises(ToolError) as err:
        await _set_thread_participation_impl(runtime, teams_caller, "on", "19:a;messageid=1", None)

    assert "only available on" not in str(err.value), "Teams is not a wrong platform"


def test_both_adapters_batch_on_the_shared_bounds() -> None:
    assert discord_bot._PARTICIPATION_BATCH_MAX_MESSAGES == BATCH_MAX_MESSAGES  # pyright: ignore[reportPrivateUsage]
    assert discord_bot._PARTICIPATION_BATCH_MAX_QUIET_PERIODS == BATCH_MAX_QUIET_PERIODS  # pyright: ignore[reportPrivateUsage]
    teams_bounds = {"BATCH_MAX_MESSAGES", "BATCH_MAX_QUIET_PERIODS", "ParticipationGates"}
    assert teams_bounds <= set(vars(teams_participation)), "Teams batches on the same shell"
