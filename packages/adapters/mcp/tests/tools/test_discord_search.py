"""Unit tests for the rewritten _search_messages_impl (phase 73 plan 03).

Each test calls ``_search_messages_impl`` directly with a hand-built
``AuthIdentity`` and a transport-level patched ``discord.http.HTTPClient``.
Inline route handlers per call site (no DRY across tests) so SDK-payload drift
breaks the relevant test.  Search-route handlers CAPTURE ``kwargs["params"]``
into a dict the test asserts on.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import daimon.adapters.mcp.tools.discord._search as _search_mod
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

_search_messages_impl = _search_mod._search_messages_impl  # pyright: ignore[reportPrivateUsage]

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers (per-file copies — inline at every call site per guideline:testing)
# ---------------------------------------------------------------------------

_VIEW_CHANNEL = 1 << 10  # 1024
_MANAGE_THREADS = 1 << 34  # 17179869184


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
    hit: bool = True,
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
        "hit": hit,
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


def _search_hit_payload(
    *,
    message_id: str,
    channel_id: str = "222",
    author_id: str = "42",
    content: str = "match",
    author_bot: bool = False,
    hit: bool = True,
) -> dict[str, Any]:
    """Build a search-hit message dict with ``hit`` field."""
    return _message_payload(
        message_id=message_id,
        channel_id=channel_id,
        author_id=author_id,
        content=content,
        author_bot=author_bot,
        hit=hit,
    )


def _search_response(
    *,
    messages: list[list[dict[str, Any]]] | None = None,
    total_results: int = 1,
    threads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a full search-response body."""
    return {
        "analytics_id": "x",
        "messages": messages if messages is not None else [],
        "total_results": total_results,
        "doing_deep_historical_index": False,
        "threads": threads if threads is not None else [],
        "members": [],
    }


# ---------------------------------------------------------------------------
# Standard auth-gate routes (resolve_member + resolve_channel for pre-validation)
# ---------------------------------------------------------------------------


def _standard_guild_member_routes() -> dict[str, Any]:
    """Return a dict of route.path -> payload for the standard guild/member/roles
    resolution that every search impl call triggers."""
    return {
        "/guilds/{guild_id}": _guild_payload(),
        "/guilds/{guild_id}/roles": [_everyone_role("111", _VIEW_CHANNEL)],
        "/guilds/{guild_id}/members/{member_id}": _member_payload(),
    }


# ---------------------------------------------------------------------------
# Params encoding
# ---------------------------------------------------------------------------


async def test_search_params_encoding_all_filters_server_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All filters reach the wire as query params — channel_id as repeated params."""
    captured_params: dict[str, Any] = {}

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [
                _text_channel_payload(channel_id="222"),
                _text_channel_payload(channel_id="333"),
            ]
        if route.path == "/channels/{channel_id}":
            ch_id = kwargs.get("channel_id") or route.channel_id
            return _text_channel_payload(channel_id=str(ch_id))
        if route.path == "/guilds/{guild_id}/messages/search":
            captured_params.update(kwargs.get("params", {}))
            return _search_response(
                messages=[[_search_hit_payload(message_id="m1")]],
                total_results=1,
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    result = await _search_messages_impl(
        _runtime_with_discord_token(),
        _auth(),
        content="hello",
        channel_ids=["222", "333"],
        author_types=["bot"],
        has=["image"],
        limit=10,
        offset=5,
    )
    assert captured_params == {
        "content": "hello",
        "channel_id": ["222", "333"],
        "author_type": ["bot"],
        "has": ["image"],
        "limit": 10,
        "offset": 5,
    }, f"all filters must reach the wire as server-side params; got {captured_params}"
    assert result.total_results == 1


# ---------------------------------------------------------------------------
# Limit/offset clamping
# ---------------------------------------------------------------------------


async def test_search_limit_clamped_to_max_25(monkeypatch: pytest.MonkeyPatch) -> None:
    """limit=50 is clamped to 25 on the wire."""
    captured_params: dict[str, Any] = {}

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload()]
        if route.path == "/guilds/{guild_id}/messages/search":
            captured_params.update(kwargs.get("params", {}))
            return _search_response(
                messages=[[_search_hit_payload(message_id="m1")]],
                total_results=1,
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    await _search_messages_impl(_runtime_with_discord_token(), _auth(), content="x", limit=50)
    assert captured_params["limit"] == 25, (
        f"limit=50 must clamp to 25; got {captured_params.get('limit')}"
    )


async def test_search_limit_clamped_to_min_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """limit=0 is clamped to 1 on the wire."""
    captured_params: dict[str, Any] = {}

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload()]
        if route.path == "/guilds/{guild_id}/messages/search":
            captured_params.update(kwargs.get("params", {}))
            return _search_response(
                messages=[[_search_hit_payload(message_id="m1")]],
                total_results=1,
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    await _search_messages_impl(_runtime_with_discord_token(), _auth(), content="x", limit=0)
    assert captured_params["limit"] == 1, (
        f"limit=0 must clamp to 1; got {captured_params.get('limit')}"
    )


async def test_search_offset_clamped_to_min_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """offset=-1 is clamped to 0 on the wire."""
    captured_params: dict[str, Any] = {}

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload()]
        if route.path == "/guilds/{guild_id}/messages/search":
            captured_params.update(kwargs.get("params", {}))
            return _search_response(
                messages=[[_search_hit_payload(message_id="m1")]],
                total_results=1,
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    await _search_messages_impl(_runtime_with_discord_token(), _auth(), content="x", offset=-1)
    assert captured_params["offset"] == 0, (
        f"offset=-1 must clamp to 0; got {captured_params.get('offset')}"
    )


# ---------------------------------------------------------------------------
# No filters at all
# ---------------------------------------------------------------------------


async def test_search_no_filters_raises_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No filters at all must raise ToolError before hitting the search route."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/messages/search":
            raise AssertionError("search route must not be hit when no filters provided")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="provide at least one search filter"):
        await _search_messages_impl(_runtime_with_discord_token(), _auth())


# ---------------------------------------------------------------------------
# Pre-validation
# ---------------------------------------------------------------------------


async def test_search_pre_validation_denies_unviewable_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """channel_ids with view-deny raises ToolError before search route is hit."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload()]
        if route.path == "/channels/{channel_id}":
            # 333 has a deny overwrite
            return _text_channel_payload(
                channel_id="333",
                permission_overwrites=[
                    {"id": "111", "type": 0, "allow": "0", "deny": str(_VIEW_CHANNEL)}
                ],
            )
        if route.path == "/guilds/{guild_id}/messages/search":
            raise AssertionError("search route must not be hit when pre-validation denies")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="missing view_channel permission"):
        await _search_messages_impl(
            _runtime_with_discord_token(), _auth(), content="x", channel_ids=["333"]
        )


async def test_search_pre_validation_denies_private_thread_non_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """channel_ids=[private-thread-id] where thread-members 404s → denied before search."""

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload(channel_id="222")]
        if route.path == "/channels/{channel_id}":
            ch_id = kwargs.get("channel_id") or route.channel_id
            if str(ch_id) == "900":
                return _thread_payload(thread_id="900", parent_id="222", thread_type=12)
            return _text_channel_payload(channel_id=str(ch_id))
        if route.path == "/channels/{channel_id}/thread-members/{user_id}":
            raise discord.NotFound(MagicMock(status=404), {"message": "Unknown Member"})
        if route.path == "/guilds/{guild_id}/messages/search":
            raise AssertionError("search route must not be hit when thread denied")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="missing view_channel permission"):
        await _search_messages_impl(
            _runtime_with_discord_token(), _auth(), content="x", channel_ids=["900"]
        )


async def test_search_pre_validation_rejects_cross_guild_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """channel_ids resolving to another guild's channel is rejected before search.

    The guild check must fire even when the caller's view check would pass —
    one deployment serves many tenant guilds and channel ids must not
    validate across them.
    """

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload()]
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload(channel_id="555", guild_id="999")
        if route.path == "/guilds/{guild_id}/messages/search":
            raise AssertionError("search route must not be hit for a cross-guild channel")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="channel not in this guild"):
        await _search_messages_impl(
            _runtime_with_discord_token(), _auth(), content="x", channel_ids=["555"]
        )


# ---------------------------------------------------------------------------
# 202 detection
# ---------------------------------------------------------------------------


async def test_search_202_index_building_raises_retry_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """202 body {code: 110000, retry_after: 3} raises the locked retry string."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload()]
        if route.path == "/guilds/{guild_id}/messages/search":
            return {"code": 110000, "message": "Index not yet available.", "retry_after": 3}
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="Search index is building. Retry in a few seconds."):
        await _search_messages_impl(_runtime_with_discord_token(), _auth(), content="x")


# ---------------------------------------------------------------------------
# Malformed body
# ---------------------------------------------------------------------------


async def test_search_malformed_body_raises_loud_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body missing 'messages' and without 202 markers raises a loud shape error."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload()]
        if route.path == "/guilds/{guild_id}/messages/search":
            return {"foo": "bar"}
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="unexpected search response format"):
        await _search_messages_impl(_runtime_with_discord_token(), _auth(), content="x")


# ---------------------------------------------------------------------------
# Hit selection (context messages have hit=false)
# ---------------------------------------------------------------------------


async def test_search_hit_selection_picks_hit_true_over_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inner group [context(hit=false), match(hit=true)] → returned row is the match."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload()]
        if route.path == "/guilds/{guild_id}/messages/search":
            return _search_response(
                messages=[
                    [
                        _search_hit_payload(message_id="context1", channel_id="222", hit=False),
                        _search_hit_payload(message_id="match1", channel_id="222", hit=True),
                    ]
                ],
                total_results=1,
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    result = await _search_messages_impl(_runtime_with_discord_token(), _auth(), content="x")
    assert len(result.rows) == 1
    assert result.rows[0].id == "match1", "must select the hit=true entry, not the context message"


# ---------------------------------------------------------------------------
# Thread-hit visibility (THE bug-1 regression test)
# ---------------------------------------------------------------------------


async def test_search_thread_hit_survives_visibility_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hit in a thread (absent from fetch_channels) is included when parent is viewable.

    This is the bug-1 regression test: thread hits were silently dropped because
    the visibility map from fetch_channels() lacked threads.
    """

    # The thread (900) is NOT in fetch_channels(); the response threads[] array
    # carries its parent_id (222, viewable).
    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            # Only channel 222; thread 900 is absent
            return [_text_channel_payload(channel_id="222")]
        if route.path == "/guilds/{guild_id}/messages/search":
            return _search_response(
                messages=[[_search_hit_payload(message_id="m1", channel_id="900")]],
                total_results=1,
                threads=[
                    _thread_payload(thread_id="900", parent_id="222", thread_type=11),
                ],
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    result = await _search_messages_impl(_runtime_with_discord_token(), _auth(), content="x")
    assert len(result.rows) == 1, (
        "thread hit must survive visibility filter when parent is viewable"
    )
    assert result.rows[0].id == "m1"


# ---------------------------------------------------------------------------
# Private-thread hit dropped
# ---------------------------------------------------------------------------


async def test_search_private_thread_hit_dropped_for_non_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private-thread hit (type 12) where thread-members 404s is silently dropped."""

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload(channel_id="222")]
        if route.path == "/guilds/{guild_id}/messages/search":
            # total_results=30 > consumed groups: the pre-fix code emitted a
            # pagination hint here that revealed hidden matches exist.
            return _search_response(
                messages=[[_search_hit_payload(message_id="m1", channel_id="900")]],
                total_results=30,
                threads=[
                    _thread_payload(thread_id="900", parent_id="222", thread_type=12),
                ],
            )
        if route.path == "/channels/{channel_id}":
            ch_id = kwargs.get("channel_id") or route.channel_id
            if str(ch_id) == "900":
                return _thread_payload(thread_id="900", parent_id="222", thread_type=12)
            return _text_channel_payload(channel_id=str(ch_id))
        if route.path == "/channels/{channel_id}/thread-members/{user_id}":
            raise discord.NotFound(MagicMock(status=404), {"message": "Unknown Member"})
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    result = await _search_messages_impl(_runtime_with_discord_token(), _auth(), content="x")
    assert len(result.rows) == 0, "private-thread hit must be silently dropped for non-member"
    assert result.total_results == 0, (
        "unscoped search must not reveal the count of matches in channels the caller cannot view"
    )
    assert result.hint is None, (
        "unscoped all-hidden page must not hint that hidden matches exist — "
        "that would recreate the count oracle the suppressed total closes"
    )


# ---------------------------------------------------------------------------
# Unknown channel hit fallback (absent from both fetch_channels and threads[])
# ---------------------------------------------------------------------------


async def test_search_unknown_channel_hit_resolves_via_fetch_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hit with channel_id absent from both fetch_channels and threads[] is resolved
    via _resolve_channel — never default-invisible."""

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            # Channel 901 is absent from fetch_channels
            return [_text_channel_payload(channel_id="222")]
        if route.path == "/guilds/{guild_id}/messages/search":
            return _search_response(
                messages=[[_search_hit_payload(message_id="m1", channel_id="901")]],
                total_results=1,
                # No threads[] entry for 901
            )
        if route.path == "/channels/{channel_id}":
            ch_id = kwargs.get("channel_id") or route.channel_id
            if str(ch_id) == "901":
                return _text_channel_payload(channel_id="901")
            return _text_channel_payload(channel_id=str(ch_id))
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    result = await _search_messages_impl(_runtime_with_discord_token(), _auth(), content="x")
    assert len(result.rows) == 1, (
        "unknown-channel hit must be resolved via fetch_channel, never dropped"
    )
    assert result.rows[0].id == "m1"


# ---------------------------------------------------------------------------
# Envelope: total_results, showing, offset, hint
# ---------------------------------------------------------------------------


async def test_search_envelope_with_pagination_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """total_results=100 with one 25-hit page at offset=0 → hint contains offset=25.

    Uses channel_ids so total_results reflects the scoped count (not suppressed
    for guild-wide leak prevention).
    """
    messages = [[_search_hit_payload(message_id=f"m{i}", channel_id="222")] for i in range(25)]

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload()]
        if route.path.startswith("/channels/"):
            return _text_channel_payload()
        if route.path == "/guilds/{guild_id}/messages/search":
            return _search_response(
                messages=messages,
                total_results=100,
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    result = await _search_messages_impl(
        _runtime_with_discord_token(), _auth(), content="x", channel_ids=["222"], limit=25
    )
    assert result.total_results == 100
    assert result.showing == 25
    assert result.offset == 0
    assert result.hint is not None and "offset=25" in result.hint, (
        f"hint must suggest offset=25; got {result.hint}"
    )


async def test_search_envelope_no_hint_when_all_results_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When offset + showing >= total_results, hint is None."""
    messages = [[_search_hit_payload(message_id=f"m{i}", channel_id="222")] for i in range(5)]

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload()]
        if route.path == "/guilds/{guild_id}/messages/search":
            return _search_response(
                messages=messages,
                total_results=5,
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    result = await _search_messages_impl(_runtime_with_discord_token(), _auth(), content="x")
    assert result.total_results == 5
    assert result.showing == 5
    assert result.hint is None, "no hint when all results are shown"


async def test_search_unscoped_total_suppressed_but_visible_rows_get_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unscoped search: total_results reports only visible rows; with visible
    rows on the page the pagination hint is softened ("may be available")
    and never states the guild-wide count."""
    messages = [[_search_hit_payload(message_id=f"m{i}", channel_id="222")] for i in range(25)]

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload()]
        if route.path == "/guilds/{guild_id}/messages/search":
            return _search_response(
                messages=messages,
                total_results=100,
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    result = await _search_messages_impl(
        _runtime_with_discord_token(), _auth(), content="x", limit=25
    )
    assert result.total_results == 25, (
        "unscoped search must report only visible rows, not the guild-wide count"
    )
    assert result.showing == 25
    assert result.hint is not None and "offset=25" in result.hint, (
        f"visible rows + more upstream must still hint continuation; got {result.hint}"
    )
    assert "100" not in result.hint, "hint must not state the guild-wide count"


# ---------------------------------------------------------------------------
# Empty success
# ---------------------------------------------------------------------------


async def test_search_empty_success_no_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """messages=[], total_results=0 → SearchResult with no error."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload()]
        if route.path == "/guilds/{guild_id}/messages/search":
            return _search_response(messages=[], total_results=0)
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    result = await _search_messages_impl(_runtime_with_discord_token(), _auth(), content="x")
    assert result.total_results == 0
    assert result.showing == 0
    assert result.rows == []
    assert result.hint is None


# ---------------------------------------------------------------------------
# Rows carry author_username and role
# ---------------------------------------------------------------------------


async def test_search_rows_carry_author_username_and_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows have author_username and role ('assistant' when author.bot is true)."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path in _standard_guild_member_routes():
            return _standard_guild_member_routes()[route.path]
        if route.path == "/guilds/{guild_id}/channels":
            return [_text_channel_payload()]
        if route.path == "/guilds/{guild_id}/messages/search":
            return _search_response(
                messages=[
                    [_search_hit_payload(message_id="m1", author_bot=False)],
                    [_search_hit_payload(message_id="m2", author_bot=True)],
                ],
                total_results=2,
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    result = await _search_messages_impl(_runtime_with_discord_token(), _auth(), content="x")
    assert len(result.rows) == 2
    assert result.rows[0].author_username == "caller"
    assert result.rows[0].role == "user"
    assert result.rows[1].role == "assistant", "bot author must have role 'assistant'"
