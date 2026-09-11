"""Executable record of thread naming's deliberate Discord-only scope.

Slack threads have no title: there is nothing for an automatic rename or a
``rename_thread`` tool to set. Asserting that here, rather than leaving it
as prose, makes the omission fail loudly if a Slack naming path appears
without this record being replaced.

No platform parametrization, no database -- this is a scope check, not a
scenario test.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import daimon.adapters.slack
import daimon.core.thread_naming
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.channels import register_channel_tools
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.scope import DeploymentDefault
from fastmcp import FastMCP
from pydantic import SecretStr


def test_no_slack_thread_naming_module_exists() -> None:
    assert importlib.util.find_spec("daimon.adapters.slack.thread_naming") is None, (
        "a Slack thread-naming module has appeared -- Slack threads have no title, so "
        "if Slack ever grows one, replace this exemption record rather than deleting it"
    )


def test_slack_adapter_source_never_reaches_for_thread_naming() -> None:
    """A Slack naming path would most likely be wired into app.py rather than
    a new module, so the module check above is not enough on its own."""
    slack_root = Path(daimon.adapters.slack.__file__).parent
    offenders = sorted(
        str(path.relative_to(slack_root))
        for path in slack_root.rglob("*.py")
        if "thread_naming" in path.read_text() or "rename_thread" in path.read_text()
    )
    assert offenders == [], (
        f"Slack adapter files reference thread naming: {offenders} -- Slack threads have no "
        "title; if that changed, replace this record rather than deleting it"
    )


def test_thread_naming_core_documents_the_discord_only_scope() -> None:
    doc = daimon.core.thread_naming.__doc__

    assert doc is not None, "daimon.core.thread_naming must carry a module docstring"
    assert "Discord-only" in doc, "the docstring must state the Discord-only scope"
    assert "test_thread_naming_discord_only" in doc, (
        "the docstring must name this test file as the executable record"
    )


async def test_rename_thread_tool_is_tagged_discord_only() -> None:
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
    assert tools["rename_thread"].tags == {"discord"}, (
        "rename_thread must stay hidden from Slack callers; a slack tag means a Slack "
        "rename path exists and this record must be replaced"
    )
