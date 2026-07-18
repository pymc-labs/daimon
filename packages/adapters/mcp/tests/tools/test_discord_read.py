"""Unit tests for Discord read-tool implementations.

Each test calls the private ``_*_impl`` functions directly with a hand-built
``AuthIdentity`` and a transport-level patched ``discord.http.HTTPClient``.
Inline route handlers per call site (no DRY across tests) so SDK-payload drift
breaks the relevant test.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import daimon.adapters.mcp.tools.discord._read as _read_mod
import discord
import discord.http
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from fastmcp.exceptions import ToolError
from pydantic import SecretStr

# Load patch_discord_http directly from the sibling conftest.py by file path.
_conftest_path = Path(__file__).parent / "conftest.py"
_spec = importlib.util.spec_from_file_location("_tools_conftest", _conftest_path)
assert _spec is not None and _spec.loader is not None
_tools_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tools_conftest)
patch_discord_http = _tools_conftest.patch_discord_http

_read_channel_impl = _read_mod._read_channel_impl  # pyright: ignore[reportPrivateUsage]
_list_channels_impl = _read_mod._list_channels_impl  # pyright: ignore[reportPrivateUsage]
_get_message_impl = _read_mod._get_message_impl  # pyright: ignore[reportPrivateUsage]
_parse_link_impl = _read_mod._parse_link_impl  # pyright: ignore[reportPrivateUsage]
_read_thread_impl = _read_mod._read_thread_impl  # pyright: ignore[reportPrivateUsage]
_list_threads_impl = _read_mod._list_threads_impl  # pyright: ignore[reportPrivateUsage]

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runtime_with_discord_token() -> McpRuntime:
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


def _auth(*, external_id: str = "111", platform_user_id: str = "42") -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="discord",
        external_id=external_id,
        platform_user_id=platform_user_id,
    )


# Permission flag constants (Discord docs).
_VIEW_CHANNEL = 1 << 10  # 1024
_SEND_MESSAGES = 1 << 11  # 2048
_MANAGE_THREADS = 1 << 34  # 17179869184


def _guild_payload(*, guild_id: str = "111", owner_id: str = "1") -> dict[str, Any]:
    return {
        "id": guild_id,
        "name": "test-guild",
        "owner_id": owner_id,
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


def _everyone_role(guild_id: str, perms: int) -> dict[str, Any]:
    return {
        "id": guild_id,
        "name": "@everyone",
        "permissions": str(perms),
        "position": 0,
        "color": 0,
        "hoist": False,
        "managed": False,
        "mentionable": False,
        "flags": 0,
    }


def _member_payload(user_id: str = "42") -> dict[str, Any]:
    return {
        "user": {
            "id": user_id,
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


def _text_channel_payload(
    *,
    channel_id: str = "222",
    guild_id: str = "111",
    permission_overwrites: list[dict[str, Any]] | None = None,
    parent_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": channel_id,
        "type": 0,
        "guild_id": guild_id,
        "name": "general",
        "position": 0,
        "permission_overwrites": permission_overwrites or [],
        "nsfw": False,
        "rate_limit_per_user": 0,
        "parent_id": parent_id,
    }


def _voice_channel_payload(  # pyright: ignore[reportUnusedFunction]
    *,
    channel_id: str = "555",
    guild_id: str = "111",
    parent_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": channel_id,
        "type": 2,
        "guild_id": guild_id,
        "name": "voice",
        "position": 0,
        "permission_overwrites": [],
        "bitrate": 64000,
        "user_limit": 0,
        "rate_limit_per_user": 0,
        "parent_id": parent_id,
        "nsfw": False,
        "rtc_region": None,
        "video_quality_mode": 1,
        "last_message_id": None,
    }


def _category_channel_payload(  # pyright: ignore[reportUnusedFunction]
    *,
    channel_id: str = "600",
    guild_id: str = "111",
) -> dict[str, Any]:
    return {
        "id": channel_id,
        "type": 4,
        "guild_id": guild_id,
        "name": "category",
        "position": 0,
        "permission_overwrites": [],
        "nsfw": False,
    }


def _author_payload(author_id: str = "42", *, bot: bool = False) -> dict[str, Any]:
    return {
        "id": author_id,
        "username": "caller",
        "discriminator": "0001",
        "global_name": "caller",
        "avatar": None,
        "bot": bot,
        "flags": 0,
    }


def _message_payload(
    *,
    message_id: str,
    channel_id: str = "222",
    author_id: str = "42",
    content: str = "hello",
    timestamp: str = "2026-05-09T00:00:00+00:00",
    attachments: list[dict[str, Any]] | None = None,
    author_bot: bool = False,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": channel_id,
        "author": _author_payload(author_id, bot=author_bot),
        "content": content,
        "timestamp": timestamp,
        "edited_timestamp": None,
        "tts": False,
        "mention_everyone": False,
        "mentions": [],
        "mention_roles": [],
        "attachments": attachments or [],
        "embeds": [],
        "type": 0,
        "pinned": False,
        "flags": 0,
    }


def _thread_payload(
    *,
    thread_id: str = "444",
    parent_id: str = "222",
    guild_id: str = "111",
    name: str = "test-thread",
    thread_type: int = 11,  # 11=public_thread, 12=private_thread
    archived: bool = False,
    message_count: int = 5,
    last_message_id: str | None = "1099",
    archive_timestamp: str = "2026-01-01T00:00:00+00:00",
) -> dict[str, Any]:
    return {
        "id": thread_id,
        "parent_id": parent_id,
        "owner_id": "1",
        "name": name,
        "type": thread_type,
        "guild_id": guild_id,
        "message_count": message_count,
        "member_count": 1,
        "thread_metadata": {
            "archived": archived,
            "auto_archive_duration": 1440,
            "archive_timestamp": archive_timestamp,
        },
        "last_message_id": last_message_id,
        "rate_limit_per_user": 0,
    }


# ---------------------------------------------------------------------------
# read_channel
# ---------------------------------------------------------------------------


async def test_read_channel_happy_path_oldest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_channel returns messages oldest-first from a newest-first fake."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL | _SEND_MESSAGES)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload()
        if route.path == "/channels/{channel_id}/messages":
            return [
                _message_payload(
                    message_id="1002", content="newer", timestamp="2026-05-09T00:00:01+00:00"
                ),
                _message_payload(message_id="1001", content="older"),
            ]
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    rows = await _read_channel_impl(
        _runtime_with_discord_token(), _auth(), channel_id="222", limit=50
    )
    assert len(rows) == 2, "read_channel should return both seeded messages"
    assert rows[0].id == "1001", "first row must be the older message (oldest-first)"
    assert rows[1].id == "1002", "second row must be the newer message"
    assert rows[0].author_username == "caller", "row must carry author_username"
    assert rows[0].role == "user", "non-bot author must have role 'user'"


async def test_read_channel_bot_author_has_assistant_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Messages from bot authors get role='assistant'."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload()
        if route.path == "/channels/{channel_id}/messages":
            return [
                _message_payload(message_id="2001", author_bot=True, content="bot says hi"),
            ]
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    rows = await _read_channel_impl(
        _runtime_with_discord_token(), _auth(), channel_id="222", limit=50
    )
    assert len(rows) == 1
    assert rows[0].role == "assistant", "bot author must have role 'assistant'"
    assert rows[0].author_username == "caller"


async def test_read_channel_rejects_thread_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_channel on a thread id raises ToolError directing to read_thread."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _thread_payload()
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="use read_thread"):
        await _read_channel_impl(_runtime_with_discord_token(), _auth(), channel_id="444")


async def test_read_channel_rejects_no_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_channel denies access when caller lacks view_channel."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", 0)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload(
                permission_overwrites=[
                    {"id": "111", "type": 0, "allow": "0", "deny": str(_VIEW_CHANNEL)}
                ]
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="view_channel"):
        await _read_channel_impl(_runtime_with_discord_token(), _auth(), channel_id="222")


async def test_read_channel_rejects_cross_guild(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_channel rejects channel in a different guild."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload(channel_id="222", guild_id="999")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="channel not in this guild"):
        await _read_channel_impl(_runtime_with_discord_token(), _auth(), channel_id="222")


async def test_read_channel_rejects_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_channel rejects DM channels."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return {"id": "333", "type": 1, "recipients": [_author_payload()]}
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="dm channels are not supported"):
        await _read_channel_impl(_runtime_with_discord_token(), _auth(), channel_id="333")


# ---------------------------------------------------------------------------
# list_channels
# ---------------------------------------------------------------------------


async def test_list_channels_filters_by_view_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_channels returns only channels the caller can view."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/guilds/{guild_id}/channels":
            return [
                _text_channel_payload(channel_id="222"),
                _text_channel_payload(
                    channel_id="333",
                    permission_overwrites=[
                        {"id": "111", "type": 0, "allow": "0", "deny": str(_VIEW_CHANNEL)}
                    ],
                ),
                _text_channel_payload(channel_id="444"),
            ]
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    rows = await _list_channels_impl(_runtime_with_discord_token(), _auth())
    assert len(rows) == 2, "should return only the 2 viewable channels"
    assert {r.id for r in rows} == {"222", "444"}
    for row in rows:
        assert row.type == "text", "channel type should be 'text'"


async def test_list_channels_includes_category_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_channels populates category_id when present."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/guilds/{guild_id}/channels":
            return [
                _text_channel_payload(channel_id="222", parent_id="600"),
            ]
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    rows = await _list_channels_impl(_runtime_with_discord_token(), _auth())
    assert len(rows) == 1
    assert rows[0].category_id == "600", "category_id should be populated from parent_id"


async def test_list_channels_admin_bypasses_permission_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin callers see all channels regardless of overwrites."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            # admin role
            return [_everyone_role("111", 8)]  # 8 = administrator
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/guilds/{guild_id}/channels":
            return [
                _text_channel_payload(channel_id="222"),
                _text_channel_payload(
                    channel_id="333",
                    permission_overwrites=[
                        {"id": "111", "type": 0, "allow": "0", "deny": str(_VIEW_CHANNEL)}
                    ],
                ),
            ]
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    rows = await _list_channels_impl(_runtime_with_discord_token(), _auth())
    assert len(rows) == 2, "admin should see all channels"


# ---------------------------------------------------------------------------
# get_message
# ---------------------------------------------------------------------------


async def test_get_message_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_message returns a single MessageRow."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload()
        if route.path == "/channels/{channel_id}/messages/{message_id}":
            return _message_payload(message_id="1001", content="fetched")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    row = await _get_message_impl(
        _runtime_with_discord_token(), _auth(), channel_id="222", message_id="1001"
    )
    assert row.id == "1001"
    assert row.content == "fetched"


async def test_get_message_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_message raises ToolError when message doesn't exist."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload()
        if route.path == "/channels/{channel_id}/messages/{message_id}":
            raise discord.NotFound(MagicMock(status=404), {"message": "Unknown Message"})
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="message not found"):
        await _get_message_impl(
            _runtime_with_discord_token(), _auth(), channel_id="222", message_id="9999"
        )


async def test_get_message_thread_aware_denies_private_thread_non_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_message on a private thread denies non-members without manage_threads."""

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            # Return thread or text channel depending on the actual channel id
            ch_id = kwargs.get("channel_id") or route.channel_id
            if str(ch_id) == "444":
                return _thread_payload(thread_type=12)
            return _text_channel_payload(channel_id="222")
        if route.path == "/channels/{channel_id}/messages/{message_id}":
            raise AssertionError("must not fetch message when view denied")
        if route.path == "/channels/{channel_id}/thread-members/{user_id}":
            raise discord.NotFound(MagicMock(status=404), {"message": "Unknown Member"})
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="missing view_channel permission"):
        await _get_message_impl(
            _runtime_with_discord_token(), _auth(), channel_id="444", message_id="1001"
        )


# ---------------------------------------------------------------------------
# parse_link (pure — no I/O tests needed beyond correctness)
# ---------------------------------------------------------------------------


class TestParseLink:
    """parse_link is pure; no monkeypatch needed."""

    def test_channel_link(self) -> None:
        result = _parse_link_impl("https://discord.com/channels/111/222")
        assert result.guild_id == "111"
        assert result.channel_id == "222"
        assert result.message_id is None
        assert result.link_type == "channel"
        assert "read_channel" in result.hint

    def test_message_link(self) -> None:
        result = _parse_link_impl("https://discord.com/channels/111/222/333")
        assert result.guild_id == "111"
        assert result.channel_id == "222"
        assert result.message_id == "333"
        assert result.link_type == "message_or_thread"
        assert "read_thread" in result.hint
        assert "get_message" in result.hint

    def test_ptb_variant(self) -> None:
        result = _parse_link_impl("https://ptb.discord.com/channels/111/222/333")
        assert result.guild_id == "111"
        assert result.message_id == "333"

    def test_canary_variant(self) -> None:
        result = _parse_link_impl("https://canary.discord.com/channels/111/222")
        assert result.guild_id == "111"
        assert result.link_type == "channel"

    def test_discordapp_variant(self) -> None:
        result = _parse_link_impl("https://discordapp.com/channels/111/222/333")
        assert result.guild_id == "111"
        assert result.message_id == "333"

    def test_non_link_raises_tool_error(self) -> None:
        with pytest.raises(ToolError, match="not a recognized discord"):
            _parse_link_impl("https://example.com/not-discord")

    def test_cross_guild_hint(self) -> None:
        result = _parse_link_impl(
            "https://discord.com/channels/999/222",
            caller_guild_id="111",
        )
        assert "different server" in result.hint

    def test_same_guild_no_cross_note(self) -> None:
        result = _parse_link_impl(
            "https://discord.com/channels/111/222",
            caller_guild_id="111",
        )
        assert "different server" not in result.hint


# ---------------------------------------------------------------------------
# read_thread
# ---------------------------------------------------------------------------


async def test_read_thread_happy_path_oldest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_thread returns messages oldest-first from a newest-first fake."""

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            ch_id = kwargs.get("channel_id") or route.channel_id
            if str(ch_id) == "222":
                return _text_channel_payload(channel_id="222")
            return _thread_payload()
        if route.path == "/channels/{channel_id}/messages":
            return [
                _message_payload(message_id="1003", content="newest", channel_id="444"),
                _message_payload(
                    message_id="1002",
                    content="middle",
                    channel_id="444",
                    timestamp="2026-05-09T00:00:01+00:00",
                ),
                _message_payload(message_id="1001", content="oldest", channel_id="444"),
            ]
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    result = await _read_thread_impl(
        _runtime_with_discord_token(), _auth(), thread_id="444", limit=50
    )
    assert len(result.rows) == 3, "should return all 3 messages"
    assert result.rows[0].id == "1001", "first row must be oldest"
    assert result.rows[2].id == "1003", "last row must be newest"
    assert result.next_before is None, "no cursor when page < limit"
    assert result.hint is None, "no hint when no more messages"


async def test_read_thread_full_page_returns_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_thread with a full page returns next_before cursor and hint."""
    # Seed exactly `limit` messages to trigger the cursor
    limit = 3

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            ch_id = kwargs.get("channel_id") or route.channel_id
            if str(ch_id) == "222":
                return _text_channel_payload(channel_id="222")
            return _thread_payload()
        if route.path == "/channels/{channel_id}/messages":
            return [
                _message_payload(message_id="1003", channel_id="444"),
                _message_payload(message_id="1002", channel_id="444"),
                _message_payload(message_id="1001", channel_id="444"),
            ]
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    result = await _read_thread_impl(
        _runtime_with_discord_token(), _auth(), thread_id="444", limit=limit
    )
    assert len(result.rows) == 3
    assert result.next_before == "1001", "cursor should be the oldest message id"
    assert result.hint is not None and "before=1001" in result.hint


async def test_read_thread_with_before_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_thread passes the before cursor to the Discord API."""
    seen_params: dict[str, Any] = {}

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            ch_id = kwargs.get("channel_id") or route.channel_id
            if str(ch_id) == "222":
                return _text_channel_payload(channel_id="222")
            return _thread_payload()
        if route.path == "/channels/{channel_id}/messages":
            seen_params.update(kwargs.get("params", {}))
            return [
                _message_payload(message_id="999", channel_id="444"),
            ]
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    result = await _read_thread_impl(
        _runtime_with_discord_token(), _auth(), thread_id="444", limit=50, before="1000"
    )
    assert len(result.rows) == 1
    assert str(seen_params.get("before")) == "1000", (
        f"before cursor must reach the wire; got params: {seen_params}"
    )


async def test_read_thread_rejects_non_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_thread on a regular channel raises ToolError."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload()
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="not a thread"):
        await _read_thread_impl(_runtime_with_discord_token(), _auth(), thread_id="222")


async def test_read_thread_private_thread_denies_non_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_thread on a private thread denies non-members without manage_threads."""

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            ch_id = kwargs.get("channel_id") or route.channel_id
            if str(ch_id) == "444":
                return _thread_payload(thread_type=12)
            return _text_channel_payload(channel_id="222")
        if route.path == "/channels/{channel_id}/thread-members/{user_id}":
            raise discord.NotFound(MagicMock(status=404), {"message": "Unknown Member"})
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="missing view_channel permission"):
        await _read_thread_impl(_runtime_with_discord_token(), _auth(), thread_id="444")


# ---------------------------------------------------------------------------
# list_threads
# ---------------------------------------------------------------------------


async def test_list_threads_merges_active_and_archived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_threads merges active threads (by parent_id) with archived public threads."""

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload(channel_id="222")
        if route.path == "/guilds/{guild_id}/threads/active":
            return {
                "threads": [
                    _thread_payload(
                        thread_id="444",
                        parent_id="222",
                        name="active-1",
                        last_message_id="2001",
                    ),
                    _thread_payload(
                        thread_id="445",
                        parent_id="222",
                        name="active-2",
                        last_message_id="2002",
                    ),
                    _thread_payload(
                        thread_id="446",
                        parent_id="999",
                        name="other-parent",
                    ),
                ],
                "members": [],
            }
        if route.path == "/channels/{channel_id}/threads/archived/public":
            return {
                "threads": [
                    _thread_payload(
                        thread_id="447",
                        parent_id="222",
                        name="archived-1",
                        archived=True,
                        last_message_id=None,
                    ),
                ],
                "has_more": False,
            }
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    rows = await _list_threads_impl(_runtime_with_discord_token(), _auth(), channel_id="222")
    assert len(rows) == 3, "should return 2 active + 1 archived (other-parent excluded)"
    ids = {r.id for r in rows}
    assert ids == {"444", "445", "447"}, "should include matching active + archived"
    for row in rows:
        assert row.parent_id == "222"


async def test_list_threads_private_thread_membership_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_threads includes private threads only when caller passes membership check."""

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload(channel_id="222")
        if route.path == "/guilds/{guild_id}/threads/active":
            return {
                "threads": [
                    _thread_payload(thread_id="444", parent_id="222", thread_type=11),
                    _thread_payload(thread_id="445", parent_id="222", thread_type=12),
                ],
                "members": [],
            }
        if route.path == "/channels/{channel_id}/threads/archived/public":
            return {"threads": [], "has_more": False}
        # Private thread membership check — 404 = not a member
        if route.path == "/channels/{channel_id}/thread-members/{user_id}":
            raise discord.NotFound(MagicMock(status=404), {"message": "Unknown Member"})
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    rows = await _list_threads_impl(_runtime_with_discord_token(), _auth(), channel_id="222")
    # Public thread (444) included; private thread (445) excluded (membership 404)
    assert len(rows) == 1, "private thread should be silently omitted"
    assert rows[0].id == "444"


async def test_list_threads_rejects_voice_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_threads on a voice channel raises ToolError."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _voice_channel_payload()
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="channel does not support threads"):
        await _list_threads_impl(_runtime_with_discord_token(), _auth(), channel_id="555")


async def test_list_threads_rejects_thread_as_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_threads on a thread id raises ToolError."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _thread_payload()
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="not a channel"):
        await _list_threads_impl(_runtime_with_discord_token(), _auth(), channel_id="444")


async def test_list_threads_last_activity_from_last_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ThreadRow.last_activity uses snowflake_time(last_message_id) when present."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload(channel_id="222")
        if route.path == "/guilds/{guild_id}/threads/active":
            return {
                "threads": [
                    _thread_payload(
                        thread_id="444",
                        parent_id="222",
                        last_message_id="1099",
                        archived=False,
                    ),
                ],
                "members": [],
            }
        if route.path == "/channels/{channel_id}/threads/archived/public":
            return {"threads": [], "has_more": False}
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    rows = await _list_threads_impl(_runtime_with_discord_token(), _auth(), channel_id="222")
    assert len(rows) == 1
    # snowflake_time(1099) should produce a valid ISO timestamp
    assert "T" in rows[0].last_activity, "last_activity should be ISO-8601"


async def test_list_threads_archived_without_last_message_uses_archive_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archived thread without last_message_id uses archive_timestamp."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload(channel_id="222")
        if route.path == "/guilds/{guild_id}/threads/active":
            return {"threads": [], "members": []}
        if route.path == "/channels/{channel_id}/threads/archived/public":
            return {
                "threads": [
                    _thread_payload(
                        thread_id="447",
                        parent_id="222",
                        archived=True,
                        last_message_id=None,
                        archive_timestamp="2026-06-01T12:00:00+00:00",
                    ),
                ],
                "has_more": False,
            }
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    rows = await _list_threads_impl(_runtime_with_discord_token(), _auth(), channel_id="222")
    assert len(rows) == 1
    assert rows[0].last_activity == "2026-06-01T12:00:00+00:00", (
        "archived thread without last_message_id should use archive_timestamp"
    )
