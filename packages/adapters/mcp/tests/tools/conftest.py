"""Make parent test helpers (conftest, factories) importable from tools/ tests.

Without __init__.py in the test directories (removed to avoid pluggy plugin
registration collision with packages/core/tests), the tools/ subdirectory
needs the parent tests/ directory on sys.path for absolute imports.

Because packages/core/tests also has a ``factories`` module on sys.path
(added by core's conftest), we must insert the MCP tests directory first
AND invalidate any cached ``factories`` so the MCP-local version wins.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import discord
import discord.http
import pytest

_parent = str(Path(__file__).resolve().parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)
else:
    # Ensure it's at the front so MCP's factories shadows core's.
    sys.path.remove(_parent)
    sys.path.insert(0, _parent)

# Invalidate cached ``factories`` module from core/tests so the MCP-local
# version is found on next import.
if "factories" in sys.modules:
    _cached = sys.modules["factories"]
    if _cached.__file__ and "adapters/mcp" not in _cached.__file__:
        del sys.modules["factories"]
        importlib.invalidate_caches()


RouteHandler = Callable[[discord.http.Route, dict[str, Any]], Awaitable[Any]]


def patch_discord_http(monkeypatch: pytest.MonkeyPatch, handler: RouteHandler) -> None:
    """Patch discord.py's HTTPClient.request + static_login at the transport level.

    Per guideline:testing — never AsyncMock client.fetch_*; patch the single
    chokepoint so discord.py's real Member/TextChannel/Message constructors run
    on the stub's payload (drift detection).
    """

    async def fake_request(
        self: discord.http.HTTPClient,
        route: discord.http.Route,
        **kwargs: Any,
    ) -> Any:
        return await handler(route, kwargs)

    async def fake_static_login(self: discord.http.HTTPClient, token: str) -> dict[str, Any]:
        return {
            "id": "1",
            "username": "bot",
            "discriminator": "0001",
            "avatar": None,
            "bot": True,
        }

    monkeypatch.setattr(discord.http.HTTPClient, "request", fake_request)
    monkeypatch.setattr(discord.http.HTTPClient, "static_login", fake_static_login)
