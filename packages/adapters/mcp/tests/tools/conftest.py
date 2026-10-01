"""Shared helpers for the MCP tool tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import discord
import discord.http
import pytest

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
