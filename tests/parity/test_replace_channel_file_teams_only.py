"""Executable record of replace_channel_file's deliberate Teams-only scope.

A Teams channel's files live in SharePoint, which keeps a version history, so
an edited file can be saved over the original. A Slack or Discord upload is
immutable: neither API takes new content for a posted file, so there an edited
file is posted again. Asserting that here makes the omission fail loudly if a
Slack or Discord path appears without replacing this record. No platform
parametrization, no database: a scope check.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import daimon.adapters.mcp.tools.discord
import daimon.adapters.mcp.tools.slack
import daimon.adapters.mcp.tools.teams._files
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.channels import register_channel_tools
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.scope import DeploymentDefault
from fastmcp import FastMCP
from pydantic import SecretStr


def test_slack_and_discord_tools_never_replace_a_posted_file() -> None:
    offenders = sorted(
        path.name
        for package in (daimon.adapters.mcp.tools.slack, daimon.adapters.mcp.tools.discord)
        for path in Path(package.__file__).parent.rglob("*.py")
        if any(term in path.read_text() for term in ("replace_channel_file", "If-Match"))
    )
    assert offenders == [], (
        f"Slack or Discord tool files replace a posted file: {offenders} -- if that became "
        "possible, replace this record rather than deleting it"
    )


def test_files_module_documents_the_teams_only_scope() -> None:
    doc = daimon.adapters.mcp.tools.teams._files.__doc__  # pyright: ignore[reportPrivateUsage]

    assert doc is not None, "the Teams files module must carry a module docstring"
    assert "Teams-only" in doc, "the docstring must state the Teams-only scope"
    assert "test_replace_channel_file_teams_only" in doc, (
        "the docstring must name this test file as the executable record"
    )


async def test_replace_channel_file_tool_is_tagged_teams_only() -> None:
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
    assert tools["replace_channel_file"].tags == {"teams"}, (
        "replace_channel_file must stay hidden from Slack and Discord callers; another tag "
        "means a path exists there and this record must be replaced"
    )
