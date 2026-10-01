"""Executable record of set_display_identity's deliberate Discord-only scope.

A Slack bot's name and icon are fixed in its app manifest; the Web API has
no call for a bot to rename itself per workspace. Asserting that here makes
the omission fail loudly if a Slack path appears without replacing this
record. No platform parametrization, no database: a scope check.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import daimon.adapters.mcp.tools.discord._identity
import daimon.adapters.slack
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.channels import register_channel_tools
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.scope import DeploymentDefault
from fastmcp import FastMCP
from pydantic import SecretStr


def test_slack_adapter_source_never_reaches_for_display_identity() -> None:
    slack_root = Path(daimon.adapters.slack.__file__).parent
    offenders = sorted(
        str(path.relative_to(slack_root))
        for path in slack_root.rglob("*.py")
        if any(
            term in path.read_text()
            for term in ("display_identity", "users.profile.set", "bot_display_name_edit")
        )
    )
    assert offenders == [], (
        f"Slack adapter files reference display identity: {offenders} -- Slack bots cannot "
        "rename themselves; if that changed, replace this record rather than deleting it"
    )


def test_identity_impl_documents_the_discord_only_scope() -> None:
    doc = daimon.adapters.mcp.tools.discord._identity.__doc__  # pyright: ignore[reportPrivateUsage]

    assert doc is not None, "the identity module must carry a module docstring"
    assert "Discord-only" in doc, "the docstring must state the Discord-only scope"
    assert "test_display_identity_discord_only" in doc, (
        "the docstring must name this test file as the executable record"
    )


async def test_set_display_identity_tool_is_tagged_discord_only() -> None:
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://x/y"),  # pyright: ignore[reportArgumentType]
        anthropic=AnthropicSettings(api_key=SecretStr("k")),
    )
    runtime = McpRuntime(
        session_factory=MagicMock(),  # type: ignore[arg-type]  # unused by registration
        client=MagicMock(),  # type: ignore[arg-type]  # unused by registration
        settings=settings,
        deployment_default=DeploymentDefault(),
    )
    mcp = FastMCP(name="parity")
    register_channel_tools(mcp, runtime)

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert tools["set_display_identity"].tags == {"discord"}, (
        "set_display_identity must stay hidden from Slack callers; a slack tag means a Slack "
        "path exists and this record must be replaced"
    )
