"""`_verify_scope`: the thread-participation tools trust no caller-supplied id.

Same transport-level Discord HTTP patching as `test_discord_visibility.py`, so
discord.py's real constructors run on the stub payloads.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import discord
import discord.http
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.thread_participation import (
    _verify_scope,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.thread_participation import ParticipationScope
from fastmcp.exceptions import ToolError
from pydantic import SecretStr

_conftest_path = Path(__file__).parent / "conftest.py"
_spec = importlib.util.spec_from_file_location("_tools_conftest", _conftest_path)
assert _spec is not None and _spec.loader is not None
_tools_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tools_conftest)
patch_discord_http = _tools_conftest.patch_discord_http

pytestmark = pytest.mark.asyncio

_VIEW_CHANNEL = 1 << 10
GUILD, PARENT, THREAD, CALLER = "111", "222", "999", "42"


def _runtime() -> McpRuntime:
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://x/y"),  # pyright: ignore[reportArgumentType]
        anthropic=AnthropicSettings(api_key=SecretStr("k")),
        discord=DiscordSettings(bot_token=SecretStr("test-bot-token")),
    )
    return McpRuntime(
        session_factory=MagicMock(),  # type: ignore[arg-type]
        client=MagicMock(),  # type: ignore[arg-type]
        settings=settings,
        deployment_default=DeploymentDefault(),
    )


def _auth() -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="discord",
        external_id=GUILD,
        platform_user_id=CALLER,
    )


def _guild() -> dict[str, Any]:
    return {
        "id": GUILD,
        "name": "g",
        "owner_id": "1",
        "afk_timeout": 0,
        "verification_level": 0,
        "default_message_notifications": 0,
        "explicit_content_filter": 0,
        "roles": [],
        "emojis": [],
        "features": [],
        "mfa_level": 0,
        "system_channel_flags": 0,
        "premium_tier": 0,
        "preferred_locale": "en-US",
        "nsfw_level": 0,
        "premium_progress_bar_enabled": False,
        "stickers": [],
        "region": "us-east",
    }


def _member() -> dict[str, Any]:
    return {
        "user": {
            "id": CALLER,
            "username": "caller",
            "discriminator": "0001",
            "global_name": "caller",
            "avatar": None,
            "bot": False,
            "flags": 0,
        },
        "roles": [],
        "joined_at": "2024-01-01T00:00:00+00:00",
        "deaf": False,
        "mute": False,
        "flags": 0,
    }


def _text_channel(channel_id: str = PARENT) -> dict[str, Any]:
    return {
        "id": channel_id,
        "type": 0,
        "guild_id": GUILD,
        "name": "general",
        "position": 0,
        "permission_overwrites": [],
        "nsfw": False,
        "rate_limit_per_user": 0,
        "parent_id": None,
    }


def _thread(*, guild_id: str = GUILD, thread_type: int = 11) -> dict[str, Any]:
    return {
        "id": THREAD,
        "parent_id": PARENT,
        "owner_id": "1",
        "name": "t",
        "type": thread_type,
        "message_count": 5,
        "member_count": 2,
        "thread_metadata": {
            "archived": False,
            "auto_archive_duration": 1440,
            "archive_timestamp": "2026-05-09T00:00:00+00:00",
        },
        "guild_id": guild_id,
    }


def _handler(*, thread_guild: str = GUILD, thread_type: int = 11, member_of_thread: bool = True):
    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild()
        if route.path == "/guilds/{guild_id}/roles":
            return [
                {
                    "id": GUILD,
                    "name": "@everyone",
                    "permissions": str(_VIEW_CHANNEL),
                    "position": 0,
                    "color": 0,
                    "hoist": False,
                    "managed": False,
                    "mentionable": False,
                    "flags": 0,
                }
            ]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member()
        if route.path == "/channels/{channel_id}":
            channel_id = str(getattr(route, "channel_id", ""))
            if channel_id == THREAD:
                return _thread(guild_id=thread_guild, thread_type=thread_type)
            return _text_channel(channel_id)
        if route.path == "/channels/{channel_id}/thread-members/{user_id}":
            if member_of_thread:
                return {
                    "id": THREAD,
                    "user_id": CALLER,
                    "join_timestamp": "2024-01-01T00:00:00+00:00",
                    "flags": 0,
                }
            raise discord.NotFound(MagicMock(status=404), {"message": "Unknown Member"})
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    return handler


async def test_a_visible_thread_yields_its_real_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_discord_http(monkeypatch, _handler())
    parent = await _verify_scope(_runtime(), _auth(), ParticipationScope.THREAD, THREAD)
    assert parent == PARENT, "the cascade must resolve against the parent Discord reports"


async def test_a_thread_in_another_guild_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_discord_http(monkeypatch, _handler(thread_guild="555"))
    with pytest.raises(ToolError, match="not in this guild"):
        await _verify_scope(_runtime(), _auth(), ParticipationScope.THREAD, THREAD)


async def test_a_private_thread_the_caller_is_not_in_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_discord_http(monkeypatch, _handler(thread_type=12, member_of_thread=False))
    with pytest.raises(ToolError, match="missing view_channel permission"):
        await _verify_scope(_runtime(), _auth(), ParticipationScope.THREAD, THREAD)


async def test_channel_scope_rejects_a_thread_id(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_discord_http(monkeypatch, _handler())
    with pytest.raises(ToolError, match="names a thread"):
        await _verify_scope(_runtime(), _auth(), ParticipationScope.CHANNEL, THREAD)


async def test_workspace_scope_needs_no_lookup() -> None:
    assert await _verify_scope(_runtime(), _auth(), ParticipationScope.WORKSPACE, None) is None
