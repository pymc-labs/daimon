"""Tool text the model reads must not name one platform's words on a shared tool.

A tool visible on Discord, Slack and Teams cannot say "Manage Server" or point
at `/agent-setup`: two of the three platforms have neither. Per-platform words
come from the `words` block in `<turn_controls>` and from tool results. A tool
tagged for exactly one platform may keep that platform's words.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, Literal

import pytest
from daimon.adapters.mcp.hub.app import build_hub_app
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.scope import DeploymentDefault
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.transforms import Visibility
from fastmcp.tools import Tool
from mux.testing.drivers.anthropic.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_tool_schema_snapshot import (
    _fully_configured_settings,  # pyright: ignore[reportPrivateUsage]
)

_PLATFORM_TAGS = frozenset({"discord", "slack", "teams"})

#: Wording that is right on one platform and wrong on the others.
_PLATFORM_WORDS = re.compile(
    r"Manage Server"
    r"|server admin"
    r"|workspace admin"
    # A slash command: not preceded by a word character or another slash, so a
    # path or URL such as `/mnt/memory` or `github.com/owner/repo` never matches.
    r"|(?<![\w/.])/(?:agent-setup|here|billing|github|dm-me|routines|memory|privacy|help)\b",
    re.IGNORECASE,
)


def _shared(tool: Tool) -> bool:
    """True when callers on more than one platform can see the tool."""
    return len(tool.tags & _PLATFORM_TAGS) != 1


def _descriptions(node: Any) -> Iterator[str]:
    """Every `description` string in a JSON schema, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():  # pyright: ignore[reportUnknownVariableType]
            if key == "description" and isinstance(value, str):
                yield value
            else:
                yield from _descriptions(value)
    elif isinstance(node, list):
        for item in node:  # pyright: ignore[reportUnknownVariableType]
            yield from _descriptions(item)


def _offences(tools: list[Tool]) -> list[str]:
    found: list[str] = []
    for tool in tools:
        if not _shared(tool):
            continue
        texts = [tool.description or "", *_descriptions(tool.parameters)]
        for text in texts:
            found.extend(
                f"{tool.name}: {match.group(0)!r}" for match in _PLATFORM_WORDS.finditer(text)
            )
    return found


async def test_shared_tool_text_names_no_platform_word(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_mcp_app(
        settings=_fully_configured_settings(),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=build_stub_anthropic(),
    )
    tools = list(await app.state.mcp.local_provider.list_tools())
    assert any(not _shared(tool) for tool in tools), "single-platform tools should be exempt"

    assert _offences(tools) == []


async def test_thread_participation_tools_are_hidden_from_slack(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Slack callers are always refused, so Slack must not list the tools."""
    app = create_mcp_app(
        settings=_fully_configured_settings(),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=build_stub_anthropic(),
    )
    tools = {tool.name: tool for tool in await app.state.mcp.local_provider.list_tools()}

    for name in ("set_thread_participation", "get_thread_participation"):
        assert tools[name].tags & _PLATFORM_TAGS == {"discord", "teams"}


async def test_hub_tool_text_names_no_platform_word(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    hub = build_hub_app(
        platform="discord",
        runtime=McpRuntime(
            session_factory=sessionmaker,
            client=build_stub_anthropic(),
            settings=_fully_configured_settings(),
            deployment_default=DeploymentDefault(environment_name=None),
        ),
        auth=StaticTokenVerifier(tokens={}),
        billing_config=None,
    )
    tools = list(await hub.local_provider.list_tools())

    assert _offences(tools) == []


def test_pattern_catches_each_banned_phrase() -> None:
    """The scan itself: it flags the words and leaves paths and URLs alone."""
    banned = [
        "Requires Manage Server (admin).",
        "needs a server admin",
        "a workspace admin must",
        "points to Add skill in /agent-setup.",
        "the same as /here",
        "top up with /billing",
        "run /github connect",
        "use /dm-me",
        "open /routines",
        "see /memory",
        "reconnect via /privacy",
        "type /help",
    ]
    for text in banned:
        assert _PLATFORM_WORDS.search(text), text

    allowed = [
        "/mnt/memory/notes.md",
        "https://github.com/owner/repo",
        "gh repo clone owner/repo",
        "an admin may change it",
        "the setup command (see `words.setup` in `<turn_controls>`)",
    ]
    for text in allowed:
        assert not _PLATFORM_WORDS.search(text), text


_CHAT_APPS = ("discord", "slack", "teams")


def _visible_on(tools: list[Tool], platform: str) -> list[Tool]:
    """The tools a turn on `platform` is offered, as `McpIdentityMiddleware` leaves them.

    `server.py` hides every platform tag by default and the middleware re-enables only
    the caller's own, so a tool tagged for several platforms shows on each of them and
    an untagged one shows everywhere."""
    return [tool for tool in tools if not tool.tags & _PLATFORM_TAGS or platform in tool.tags]


def _texts(tool: Tool) -> list[str]:
    return [tool.name, tool.description or "", *_descriptions(tool.parameters)]


def _foreign_names(texts: list[str], platform: str) -> list[str]:
    others = [app for app in _CHAT_APPS if app != platform]
    return [
        f"{other!r} in {text[:80]!r}" for text in texts for other in others if other in text.lower()
    ]


@pytest.mark.parametrize("platform", _CHAT_APPS)
async def test_a_turn_sees_no_other_chat_platform_in_its_tool_list(
    platform: str, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    app = create_mcp_app(
        settings=_fully_configured_settings(),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=build_stub_anthropic(),
    )
    mcp = app.state.mcp
    hidden = {
        tag
        for transform in mcp.transforms
        if isinstance(transform, Visibility) and not transform._enabled  # pyright: ignore[reportPrivateUsage]
        for tag in (transform.tags or ())
    }
    assert hidden >= _PLATFORM_TAGS, "server.py must hide every platform tag by default"
    assert mcp.instructions is None, "the server has no instructions that could name a platform"

    tools = _visible_on(list(await mcp.local_provider.list_tools()), platform)
    assert tools, platform
    assert [t.name for t in tools if _foreign_names(_texts(t), platform)] == []


@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_a_hub_login_sees_no_other_chat_platform(
    platform: Literal["discord", "slack"], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    hub = build_hub_app(
        platform=platform,
        runtime=McpRuntime(
            session_factory=sessionmaker,
            client=build_stub_anthropic(),
            settings=_fully_configured_settings(),
            deployment_default=DeploymentDefault(environment_name=None),
        ),
        auth=StaticTokenVerifier(tokens={}),
        billing_config=None,
    )
    texts = [hub.instructions or ""]
    for tool in await hub.local_provider.list_tools():
        texts.extend(_texts(tool))

    assert _foreign_names(texts, platform) == []
