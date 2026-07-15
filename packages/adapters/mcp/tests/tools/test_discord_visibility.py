"""Unit tests for _check_thread_view — thread-aware visibility helper.

Each test uses a transport-level patched discord.http.HTTPClient via
patch_discord_http so discord.py's real constructors run on the stub payload.
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
from daimon.adapters.mcp.tools.discord._client import (
    _resolve_channel,
    _resolve_member,
    rest_client,
)
from daimon.adapters.mcp.tools.discord._visibility import _check_thread_view
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

_VIEW_CHANNEL = 1 << 10  # 1024
_SEND_MESSAGES = 1 << 11  # 2048
_MANAGE_THREADS = 1 << 34


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


def _thread_payload(
    *,
    thread_id: str = "999",
    parent_id: str = "222",
    guild_id: str = "111",
    thread_type: int = 11,
    archived: bool = False,
    last_message_id: str | None = None,
) -> dict[str, Any]:
    """Build a Thread payload with ALL required keys for Thread._from_data."""
    payload: dict[str, Any] = {
        "id": thread_id,
        "parent_id": parent_id,
        "owner_id": "1",
        "name": "test-thread",
        "type": thread_type,
        "message_count": 5,
        "member_count": 2,
        "thread_metadata": {
            "archived": archived,
            "auto_archive_duration": 1440,
            "archive_timestamp": "2026-05-09T00:00:00+00:00",
        },
        "guild_id": guild_id,
    }
    if last_message_id is not None:
        payload["last_message_id"] = last_message_id
    return payload


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
# Test plumbing: resolve member + channel, then exercise _check_thread_view
# ---------------------------------------------------------------------------


async def _setup_thread_test(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    *,
    thread_id: str = "999",
) -> tuple[discord.Client, discord.Thread, discord.Member]:
    """Common plumbing: patch HTTP, resolve member + channel, return (client, thread, member)."""
    patch_discord_http(monkeypatch, handler)
    async with rest_client("test-token") as c:
        guild, member = await _resolve_member(c, "111", "42")
        thread_raw = await _resolve_channel(c, thread_id)
        assert isinstance(thread_raw, discord.Thread), (
            f"expected Thread, got {type(thread_raw).__name__}"
        )
        return c, thread_raw, member


# ---------------------------------------------------------------------------
# Test 1: public thread + parent grants view_channel -> passes, no thread-members probe
# ---------------------------------------------------------------------------


async def test_public_thread_passes_when_parent_grants_view_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public thread (type 11) with parent view_channel -> _check_thread_view returns
    without raising and without hitting the thread-members route."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            # First call returns parent channel (222), second returns thread (999)
            # _resolve_channel is called with "999" so it fetches the thread
            return _thread_payload(thread_type=11)
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    c, thread, member = await _setup_thread_test(monkeypatch, handler)

    # Register the parent channel in the guild cache so thread.parent resolves
    parent = discord.TextChannel(
        state=c._connection,
        guild=thread.guild,
        data=_text_channel_payload(channel_id="222", guild_id="111"),
    )
    thread.guild._add_channel(parent)  # pyright: ignore[reportPrivateUsage]

    # Should pass without raising
    await _check_thread_view(c, thread, member, "42")


# ---------------------------------------------------------------------------
# Test 2: parent denies view_channel -> ToolError
# ---------------------------------------------------------------------------


async def test_thread_denied_when_parent_denies_view_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent channel denies view_channel via overwrite -> ToolError."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", 0)]  # no permissions
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _thread_payload(thread_type=11)
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    c, thread, member = await _setup_thread_test(monkeypatch, handler)

    # Register parent with deny overwrite
    parent = discord.TextChannel(
        state=c._connection,
        guild=thread.guild,
        data=_text_channel_payload(
            channel_id="222",
            guild_id="111",
            permission_overwrites=[
                {"id": "111", "type": 0, "allow": "0", "deny": str(_VIEW_CHANNEL)}
            ],
        ),
    )
    thread.guild._add_channel(parent)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(ToolError, match="missing view_channel permission"):
        await _check_thread_view(c, thread, member, "42")


# ---------------------------------------------------------------------------
# Test 3: private thread, no manage_threads, thread-members 404 -> same error string
# ---------------------------------------------------------------------------


async def test_private_thread_denied_when_not_member_and_no_manage_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private thread (type 12), caller lacks manage_threads, thread-members returns 404
    -> ToolError with IDENTICAL string (no existence leak) AND handler saw the route."""

    thread_members_route_hit = False

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        nonlocal thread_members_route_hit
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _thread_payload(thread_type=12)  # private thread
        if route.path == "/channels/{channel_id}/thread-members/{user_id}":
            thread_members_route_hit = True
            raise discord.NotFound(MagicMock(status=404), {"message": "Unknown Member"})
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    c, thread, member = await _setup_thread_test(monkeypatch, handler)

    # Register parent with view_channel
    parent = discord.TextChannel(
        state=c._connection,
        guild=thread.guild,
        data=_text_channel_payload(channel_id="222", guild_id="111"),
    )
    thread.guild._add_channel(parent)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(ToolError, match="missing view_channel permission"):
        await _check_thread_view(c, thread, member, "42")

    assert thread_members_route_hit, (
        "thread-members route must be probed for private thread membership"
    )


# ---------------------------------------------------------------------------
# Test 4: private thread, parent grants manage_threads -> passes, no thread-members call
# ---------------------------------------------------------------------------


async def test_private_thread_passes_with_manage_threads_perm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private thread, parent grants manage_threads -> passes WITHOUT calling thread-members."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL | _MANAGE_THREADS)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _thread_payload(thread_type=12)
        if route.path == "/channels/{channel_id}/thread-members/{user_id}":
            raise AssertionError(
                "thread-members route must not be called when manage_threads granted"
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    c, thread, member = await _setup_thread_test(monkeypatch, handler)

    # Register parent
    parent = discord.TextChannel(
        state=c._connection,
        guild=thread.guild,
        data=_text_channel_payload(channel_id="222", guild_id="111"),
    )
    thread.guild._add_channel(parent)  # pyright: ignore[reportPrivateUsage]

    # Should pass without raising
    await _check_thread_view(c, thread, member, "42")


# ---------------------------------------------------------------------------
# Test 5: private thread, no manage_threads, thread-members succeeds -> passes
# ---------------------------------------------------------------------------


async def test_private_thread_passes_when_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private thread, no manage_threads, but thread-members route returns valid payload -> passes."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _thread_payload(thread_type=12)
        if route.path == "/channels/{channel_id}/thread-members/{user_id}":
            return {
                "id": "999",
                "user_id": "42",
                "join_timestamp": "2026-05-09T00:00:00+00:00",
                "flags": 0,
            }
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    c, thread, member = await _setup_thread_test(monkeypatch, handler)

    # Register parent
    parent = discord.TextChannel(
        state=c._connection,
        guild=thread.guild,
        data=_text_channel_payload(channel_id="222", guild_id="111"),
    )
    thread.guild._add_channel(parent)  # pyright: ignore[reportPrivateUsage]

    # Should pass without raising
    await _check_thread_view(c, thread, member, "42")


# ---------------------------------------------------------------------------
# Test 6: administrator -> passes with no parent fetch and no membership probe
# ---------------------------------------------------------------------------


async def test_admin_bypasses_all_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller with administrator permission -> passes with no parent fetch and no membership probe."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", 1 << 3)]  # ADMINISTRATOR
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _thread_payload(thread_type=12)
        if route.path == "/channels/{channel_id}/thread-members/{user_id}":
            raise AssertionError("thread-members route must not be called for admin")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    c, thread, member = await _setup_thread_test(monkeypatch, handler)

    # Should pass without raising — admin bypasses everything
    await _check_thread_view(c, thread, member, "42")
