"""Unit tests for the Discord thread implementations (_create_thread_impl,
_rename_thread_impl).

Each test calls the private impl directly with a hand-built
``AuthIdentity`` and a transport-level patched ``discord.http.HTTPClient``.
Inline route handlers and payload helpers per this file (no cross-test-file
imports) so SDK-payload drift breaks the relevant test.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import discord
import discord.http
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._threads import (
    _create_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _rename_thread_impl,  # pyright: ignore[reportPrivateUsage]
)
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

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Permission flag constants (Discord docs).
# ---------------------------------------------------------------------------

_VIEW_CHANNEL = 1 << 10
_SEND_MESSAGES = 1 << 11
_MANAGE_THREADS = 1 << 34
_CREATE_PUBLIC_THREADS = 1 << 35
_SEND_MESSAGES_IN_THREADS = 1 << 38

# The fake ``static_login`` in conftest reports the bot as user "1"; a thread
# with that owner_id is one daimon opened, any other owner is a human's.
_BOT_USER_ID = "1"

# A real-looking snowflake (2026-ish), deliberately far from the tiny thread_id
# ("444") used below, so a last_activity that used the thread's own snowflake
# instead of the starter message's would produce a visibly different value.
_STARTER_MESSAGE_ID = "1400000000000000000"


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


# ---------------------------------------------------------------------------
# Payload helpers (per-file, no cross-test-file imports)
# ---------------------------------------------------------------------------


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
        "parent_id": None,
    }


def _forum_channel_payload(
    *,
    channel_id: str = "333",
    guild_id: str = "111",
    permission_overwrites: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": channel_id,
        "type": 15,
        "guild_id": guild_id,
        "name": "forum",
        "position": 0,
        "permission_overwrites": permission_overwrites or [],
        "nsfw": False,
        "rate_limit_per_user": 0,
        "parent_id": None,
    }


def _voice_channel_payload(*, channel_id: str = "555", guild_id: str = "111") -> dict[str, Any]:
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
        "parent_id": None,
        "nsfw": False,
        "rtc_region": None,
        "video_quality_mode": 1,
        "last_message_id": None,
    }


def _thread_payload(
    *,
    thread_id: str = "444",
    parent_id: str = "222",
    guild_id: str = "111",
    name: str = "test-thread",
    thread_type: int = 11,
    archived: bool = False,
    message_count: int = 0,
    last_message_id: str | None = None,
    owner_id: str = "1",
) -> dict[str, Any]:
    return {
        "id": thread_id,
        "parent_id": parent_id,
        "owner_id": owner_id,
        "name": name,
        "type": thread_type,
        "guild_id": guild_id,
        "message_count": message_count,
        "member_count": 1,
        "thread_metadata": {
            "archived": archived,
            "auto_archive_duration": 1440,
            "archive_timestamp": "2026-01-01T00:00:00+00:00",
        },
        "last_message_id": last_message_id,
        "rate_limit_per_user": 0,
    }


def _author_payload(author_id: str = "1", *, bot: bool = True) -> dict[str, Any]:
    return {
        "id": author_id,
        "username": "daimon",
        "discriminator": "0001",
        "global_name": "daimon",
        "avatar": None,
        "bot": bot,
        "flags": 0,
    }


def _message_payload(
    *,
    message_id: str,
    channel_id: str,
    content: str = "hello",
    timestamp: str = "2026-05-09T00:00:00+00:00",
) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": channel_id,
        "author": _author_payload(),
        "content": content,
        "timestamp": timestamp,
        "edited_timestamp": None,
        "tts": False,
        "mention_everyone": False,
        "mentions": [],
        "mention_roles": [],
        "attachments": [],
        "embeds": [],
        "type": 0,
        "pinned": False,
        "flags": 0,
    }


# ---------------------------------------------------------------------------
# Text channel: happy path + C-1 regression guard
# ---------------------------------------------------------------------------


async def test_create_thread_text_channel_happy_path_returns_thread_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text channel: creates the thread, posts the starter message, and returns
    a ThreadRow with message_count=1 and last_activity from the starter
    message — NOT 0, NOT the thread's own snowflake (the C-5 guard)."""

    captured_create_kwargs: dict[str, Any] = {}

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [
                _everyone_role(
                    "111",
                    _VIEW_CHANNEL | _CREATE_PUBLIC_THREADS | _SEND_MESSAGES_IN_THREADS,
                )
            ]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}" and route.method == "GET":
            return _text_channel_payload(channel_id="222")
        if route.path == "/channels/{channel_id}/threads" and route.method == "POST":
            captured_create_kwargs.update(kwargs)
            return _thread_payload(thread_id="444", parent_id="222", message_count=0)
        if route.path == "/channels/{channel_id}/messages" and route.method == "POST":
            assert route.channel_id == 444, "starter message must post into the new thread"
            return _message_payload(
                message_id=_STARTER_MESSAGE_ID,
                channel_id="444",
                content="hi there",
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    row = await _create_thread_impl(
        _runtime_with_discord_token(),
        _auth(),
        channel_id="222",
        name="my thread",
        content="hi there",
    )

    assert row.id == "444", "row id must be the created thread's id"
    assert row.name == "test-thread", "row name must be the created thread's name"
    assert row.parent_id == "222", "row parent_id must be the text channel"
    assert row.archived is False, "a freshly created thread must not be archived"
    assert row.message_count == 1, "row must report the starter message (C-5 guard)"
    expected_last_activity = discord.utils.snowflake_time(int(_STARTER_MESSAGE_ID)).isoformat()
    assert row.last_activity == expected_last_activity, (
        "last_activity must derive from the starter message's own snowflake, not 0 and not "
        "the thread's own snowflake (C-5 guard)"
    )
    assert row.last_activity != discord.utils.snowflake_time(444).isoformat(), (
        "last_activity must NOT be derived from the thread's own snowflake (C-5 guard)"
    )
    assert captured_create_kwargs["json"]["type"] == 11, (
        "text-channel create must pass type=11 (public_thread) explicitly (C-1 guard)"
    )


# ---------------------------------------------------------------------------
# Forum channel: happy path
# ---------------------------------------------------------------------------


async def test_create_thread_forum_channel_happy_path_returns_thread_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forum channel: a single POST carries the starter content and returns a
    ThreadRow built from ThreadWithMessage.thread + .message."""

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL | _SEND_MESSAGES)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}" and route.method == "GET":
            return _forum_channel_payload(channel_id="333")
        if route.path == "/channels/{channel_id}/threads" and route.method == "POST":
            body = cast(dict[str, Any], kwargs["json"])
            message_body = cast(dict[str, Any], body["message"])
            assert message_body["content"] == "forum post body", (
                "forum create must carry the starter content in the nested message payload"
            )
            thread_data = _thread_payload(
                thread_id="446", parent_id="333", thread_type=11, message_count=1
            )
            thread_data["message"] = _message_payload(
                message_id="446", channel_id="446", content="forum post body"
            )
            return thread_data
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    row = await _create_thread_impl(
        _runtime_with_discord_token(),
        _auth(),
        channel_id="333",
        name="forum post",
        content="forum post body",
    )

    assert row.id == "446", "row id must be the created forum thread's id"
    assert row.parent_id == "333", "row parent_id must be the forum channel"
    assert row.message_count == 1, "forum starter message counts as the thread's first message"


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


async def test_create_thread_rejects_thread_as_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing a thread id as channel_id raises a named ToolError."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}" and route.method == "GET":
            return _thread_payload(thread_id="444")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="not a channel"):
        await _create_thread_impl(
            _runtime_with_discord_token(), _auth(), channel_id="444", name="x", content="hi"
        )


async def test_create_thread_rejects_voice_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """A voice channel (or any non-text, non-forum channel) is rejected."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}" and route.method == "GET":
            return _voice_channel_payload(channel_id="555")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="channel does not support threads"):
        await _create_thread_impl(
            _runtime_with_discord_token(), _auth(), channel_id="555", name="x", content="hi"
        )


async def test_create_thread_denied_without_create_public_threads_before_any_create_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller without create_public_threads on a text channel is denied by
    Task 1's helper BEFORE any create request reaches the route handler."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}" and route.method == "GET":
            return _text_channel_payload(channel_id="222")
        if route.path == "/channels/{channel_id}/threads":
            raise AssertionError("create-thread route must not be called without permission")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="missing create_public_threads permission"):
        await _create_thread_impl(
            _runtime_with_discord_token(), _auth(), channel_id="222", name="x", content="hi"
        )


async def test_create_thread_rejects_empty_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty (or whitespace-only) name is rejected before any I/O."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        raise AssertionError(f"no I/O expected, got {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="empty"):
        await _create_thread_impl(
            _runtime_with_discord_token(), _auth(), channel_id="222", name="   ", content="hi"
        )


async def test_create_thread_rejects_name_over_100_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name over 100 characters is rejected, naming the 100-char cap."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        raise AssertionError(f"no I/O expected, got {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="100 characters"):
        await _create_thread_impl(
            _runtime_with_discord_token(),
            _auth(),
            channel_id="222",
            name="x" * 101,
            content="hi",
        )


async def test_create_thread_forbidden_from_create_maps_to_named_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A discord.Forbidden raised by the create call is mapped to a ToolError
    naming daimon's own missing permission, chained from the original error."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [
                _everyone_role(
                    "111",
                    _VIEW_CHANNEL | _CREATE_PUBLIC_THREADS | _SEND_MESSAGES_IN_THREADS,
                )
            ]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}" and route.method == "GET":
            return _text_channel_payload(channel_id="222")
        if route.path == "/channels/{channel_id}/threads" and route.method == "POST":
            raise discord.Forbidden(MagicMock(status=403), {"message": "Missing Permissions"})
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="daimon") as exc_info:
        await _create_thread_impl(
            _runtime_with_discord_token(), _auth(), channel_id="222", name="x", content="hi"
        )
    assert isinstance(exc_info.value.__cause__, discord.Forbidden), (
        "the ToolError must chain the original discord.Forbidden via `from err`"
    )


# ---------------------------------------------------------------------------
# rename_thread
# ---------------------------------------------------------------------------


def _rename_handler(
    *,
    everyone_perms: int,
    owner_id: str,
    captured_patch: dict[str, Any] | None = None,
) -> Any:
    """Route table for a rename: guild/member hydration, the thread GET, its
    parent GET (``_ensure_thread_parent_cached``) and the PATCH itself."""

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", everyone_perms)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}" and route.method == "GET":
            if route.channel_id == 444:
                return _thread_payload(thread_id="444", parent_id="222", owner_id=owner_id)
            return _text_channel_payload(channel_id="222")
        if route.path == "/channels/{channel_id}" and route.method == "PATCH":
            assert captured_patch is not None, "PATCH must not be reached in a denial test"
            assert route.channel_id == 444, "the rename must target the thread itself"
            captured_patch.update(kwargs)
            return _thread_payload(
                thread_id="444", parent_id="222", owner_id=owner_id, name="Renamed Title"
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    return handler


async def test_rename_thread_bot_owned_thread_renames_with_post_permission_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A participant (view + send_messages_in_threads, no manage_threads) may
    rename a thread daimon opened; the PATCH carries the validated name and the
    row reflects Discord's post-edit state."""
    captured: dict[str, Any] = {}
    patch_discord_http(
        monkeypatch,
        _rename_handler(
            everyone_perms=_VIEW_CHANNEL | _SEND_MESSAGES_IN_THREADS,
            owner_id=_BOT_USER_ID,
            captured_patch=captured,
        ),
    )

    row = await _rename_thread_impl(
        _runtime_with_discord_token(), _auth(), thread_id="444", name="  Renamed Title  "
    )

    assert captured["json"]["name"] == "Renamed Title", (
        "the PATCH body must carry the stripped, validated name"
    )
    assert row.id == "444" and row.name == "Renamed Title", (
        "the returned row must reflect the thread as Discord reports it after the edit"
    )


async def test_rename_thread_human_owned_thread_denied_without_manage_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Renaming someone else's thread is moderation: a mere participant is
    refused before any PATCH."""
    patch_discord_http(
        monkeypatch,
        _rename_handler(everyone_perms=_VIEW_CHANNEL | _SEND_MESSAGES_IN_THREADS, owner_id="777"),
    )
    with pytest.raises(ToolError, match="manage_threads"):
        await _rename_thread_impl(
            _runtime_with_discord_token(), _auth(), thread_id="444", name="Renamed Title"
        )


async def test_rename_thread_human_owned_thread_allowed_with_manage_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    patch_discord_http(
        monkeypatch,
        _rename_handler(
            everyone_perms=_VIEW_CHANNEL | _MANAGE_THREADS,
            owner_id="777",
            captured_patch=captured,
        ),
    )
    row = await _rename_thread_impl(
        _runtime_with_discord_token(), _auth(), thread_id="444", name="Renamed Title"
    )
    assert captured["json"]["name"] == "Renamed Title", "manage_threads must unlock the rename"
    assert row.name == "Renamed Title", "row must carry the new name"


async def test_rename_thread_bot_owned_thread_denied_without_post_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Viewing alone is not participating: a read-only member cannot rename
    even daimon's own thread."""
    patch_discord_http(
        monkeypatch, _rename_handler(everyone_perms=_VIEW_CHANNEL, owner_id=_BOT_USER_ID)
    )
    with pytest.raises(ToolError, match="send_messages_in_threads"):
        await _rename_thread_impl(
            _runtime_with_discord_token(), _auth(), thread_id="444", name="Renamed Title"
        )


async def test_rename_thread_rejects_channel_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL | _MANAGE_THREADS)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}" and route.method == "GET":
            return _text_channel_payload(channel_id="222")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="not a thread"):
        await _rename_thread_impl(
            _runtime_with_discord_token(), _auth(), thread_id="222", name="Renamed Title"
        )


async def test_rename_thread_rejects_empty_name_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        raise AssertionError(f"no request expected, got {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="must not be empty"):
        await _rename_thread_impl(
            _runtime_with_discord_token(), _auth(), thread_id="444", name="   "
        )
