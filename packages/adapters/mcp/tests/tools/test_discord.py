"""Unit tests for Discord MCP tools (MCP-05).

Each test calls the private ``_*_impl`` functions directly with a hand-built
``AuthIdentity`` and a transport-level patched ``discord.http.HTTPClient``.
Inline route handlers per call site (no DRY across tests) so SDK-payload drift
breaks the relevant test.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import daimon.adapters.mcp.tools.discord as _discord_mod
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
# Avoids the "from conftest import ..." collision with the parent tests/conftest.py.
_conftest_path = Path(__file__).parent / "conftest.py"
_spec = importlib.util.spec_from_file_location("_tools_conftest", _conftest_path)
assert _spec is not None and _spec.loader is not None
_tools_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tools_conftest)
patch_discord_http = _tools_conftest.patch_discord_http

_send_message_impl = _discord_mod._send_message_impl  # pyright: ignore[reportPrivateUsage]
_fetch_attachment = _discord_mod._fetch_attachment  # pyright: ignore[reportPrivateUsage]
_require_discord_identity = _discord_mod._require_discord_identity  # pyright: ignore[reportPrivateUsage]
_require_guild_id = _discord_mod._require_guild_id  # pyright: ignore[reportPrivateUsage]

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Tiny aiohttp fake used by the two attachment-streaming tests.
# Per RESEARCH.md "Test Strategy" — kept inline rather than lifted to
# conftest until a second tool needs it.
# ---------------------------------------------------------------------------


class _FakeAiohttpResponse:
    def __init__(self, *, content_length: int | None, chunks: list[bytes]) -> None:
        self.content_length = content_length
        self._chunks = chunks
        self.content = self  # iter_chunked is a method on .content

    async def __aenter__(self) -> _FakeAiohttpResponse:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def iter_chunked(self, _n: int) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class _FakeAiohttpSession:
    def __init__(self, response: _FakeAiohttpResponse) -> None:
        self._response = response
        self.get_kwargs: dict[str, Any] = {}

    async def __aenter__(self) -> _FakeAiohttpSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def get(self, _url: str, **kwargs: Any) -> _FakeAiohttpResponse:
        self.get_kwargs = kwargs
        return self._response


# ---------------------------------------------------------------------------
# Helpers (small, intentional)
# ---------------------------------------------------------------------------


def _runtime_with_discord_token() -> McpRuntime:
    """Build an McpRuntime with a Settings carrying a discord.bot_token.

    Settings construction is fully validated; we mock only the unrelated
    Anthropic client + sqlalchemy session_factory which the impls don't touch
    in unit tests (``rest_client`` opens its own discord HTTP).
    """
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://x/y"),  # pyright: ignore[reportArgumentType]
        anthropic=AnthropicSettings(api_key=SecretStr("k")),
        discord=DiscordSettings(bot_token=SecretStr("test-bot-token")),
    )
    return McpRuntime(
        session_factory=MagicMock(),  # type: ignore[arg-type]  # impls don't use it
        client=MagicMock(),  # type: ignore[arg-type]  # impls don't use it
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


def _author_payload(author_id: str = "42") -> dict[str, Any]:
    return {
        "id": author_id,
        "username": "caller",
        "discriminator": "0001",
        "global_name": "caller",
        "avatar": None,
        "bot": False,
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
) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": channel_id,
        "author": _author_payload(author_id),
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


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


async def test_send_message_text_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL | _SEND_MESSAGES)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload()
        if route.method == "POST" and route.path == "/channels/{channel_id}/messages":
            return _message_payload(message_id="9001", content="posted")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    row = await _send_message_impl(
        _runtime_with_discord_token(),
        _auth(),
        channel_id="222",
        content="posted",
    )
    assert row.id == "9001"
    assert row.content == "posted"
    assert row.channel_id == "222"


async def test_send_message_with_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload_bytes = b"hello-attachment"
    fake_session = _FakeAiohttpSession(
        _FakeAiohttpResponse(content_length=len(payload_bytes), chunks=[payload_bytes])
    )

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL | _SEND_MESSAGES)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload()
        if route.method == "POST" and route.path == "/channels/{channel_id}/messages":
            return _message_payload(
                message_id="9002",
                content="with-att",
                attachments=[
                    {
                        "id": "7001",
                        "filename": "x.png",
                        "url": "https://cdn.example.com/x.png",
                        "proxy_url": "https://cdn.example.com/x.png",
                        "size": len(payload_bytes),
                    }
                ],
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    row = await _send_message_impl(
        _runtime_with_discord_token(),
        _auth(),
        channel_id="222",
        content="with-att",
        attachments=[{"url": "https://cdn.discordapp.com/x.png", "filename": "x.png"}],
        session=fake_session,  # type: ignore[arg-type]  # _FakeAiohttpSession satisfies the protocol
    )
    assert row.id == "9002"
    assert len(row.attachments) == 1
    assert row.attachments[0].filename == "x.png"


async def test_send_message_attachment_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = _FakeAiohttpSession(_FakeAiohttpResponse(content_length=30_000_000, chunks=[]))

    async def handler(_route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        # Should never be called — _build_files raises before any Discord I/O.
        raise AssertionError("Discord HTTP must not be hit")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="exceeds 25"):
        await _send_message_impl(
            _runtime_with_discord_token(),
            _auth(),
            channel_id="222",
            content="x",
            attachments=[{"url": "https://cdn.discordapp.com/big", "filename": "big.bin"}],
            session=fake_session,  # type: ignore[arg-type]  # _FakeAiohttpSession satisfies the protocol
        )


async def test_send_message_attachment_bad_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pass a fake session so a stray fetch doesn't reach the network.
    # The https scheme check in _fetch_attachment raises before the session is used.
    fake_session = _FakeAiohttpSession(_FakeAiohttpResponse(content_length=0, chunks=[]))

    async def handler(_route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        raise AssertionError("Discord HTTP must not be hit")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="https"):
        await _send_message_impl(
            _runtime_with_discord_token(),
            _auth(),
            channel_id="222",
            content="x",
            attachments=[{"url": "http://example.com/x", "filename": "x.png"}],
            session=fake_session,  # type: ignore[arg-type]  # _FakeAiohttpSession satisfies the protocol
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data/",
        "https://evil.example.com/x.png",
        "https://cdn.discordapp.com.evil.com/x.png",
        "https://attacker.test/redirect-to-metadata",
    ],
)
async def test_fetch_attachment_rejects_non_discord_host(url: str) -> None:
    """SSRF guard: only Discord CDN hosts are fetchable, regardless of https scheme."""
    fake_session = _FakeAiohttpSession(_FakeAiohttpResponse(content_length=0, chunks=[]))
    with pytest.raises(ToolError, match="discord"):
        await _fetch_attachment(fake_session, url)  # type: ignore[arg-type]
    assert fake_session.get_kwargs == {}, "rejected host must never reach session.get"


async def test_fetch_attachment_disables_redirects_for_allowed_host() -> None:
    """An allowlisted host is fetched with redirects disabled so it cannot
    open-redirect to an internal target (the SSRF bypass)."""
    fake_session = _FakeAiohttpSession(_FakeAiohttpResponse(content_length=3, chunks=[b"abc"]))
    data = await _fetch_attachment(
        fake_session,  # type: ignore[arg-type]
        "https://cdn.discordapp.com/attachments/1/2/x.png",
    )
    assert data == b"abc", "allowlisted host fetch returns the body"
    assert fake_session.get_kwargs.get("allow_redirects") is False, (
        "attachment fetch must disable redirect following"
    )


async def test_fetch_attachment_converts_client_response_error_to_tool_error() -> None:
    """An HTTP error from the CDN (raise_for_status) must surface as a ToolError,
    not escape to FastMCP as an opaque internal error (#13)."""

    class _RaisingResp:
        content_length = None

        async def __aenter__(self) -> _RaisingResp:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        def raise_for_status(self) -> None:
            raise aiohttp.ClientResponseError(MagicMock(), (), status=404, message="Not Found")

    class _RaisingSession:
        def get(self, _url: str, **_kwargs: Any) -> _RaisingResp:
            return _RaisingResp()

    with pytest.raises(ToolError, match="attachment"):
        await _fetch_attachment(
            _RaisingSession(),  # type: ignore[arg-type]
            "https://cdn.discordapp.com/attachments/1/2/x.png",
        )


async def test_send_message_too_many_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(_route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        raise AssertionError("Discord HTTP must not be hit")

    patch_discord_http(monkeypatch, handler)
    too_many = [{"url": f"https://example.com/{i}.png", "filename": f"{i}.png"} for i in range(11)]
    with pytest.raises(ToolError, match="max 10"):
        await _send_message_impl(
            _runtime_with_discord_token(),
            _auth(),
            channel_id="222",
            content="x",
            attachments=too_many,
        )


async def test_send_message_no_send_perm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]  # no send_messages
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            # @everyone has view; explicit deny on send_messages
            return _text_channel_payload(
                permission_overwrites=[
                    {
                        "id": "111",
                        "type": 0,
                        "allow": "0",
                        "deny": str(_SEND_MESSAGES),
                    }
                ]
            )
        if route.method == "POST" and route.path == "/channels/{channel_id}/messages":
            raise AssertionError("must not POST when send permission denied")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="send_messages"):
        await _send_message_impl(
            _runtime_with_discord_token(),
            _auth(),
            channel_id="222",
            content="x",
        )


def _thread_payload(
    *,
    thread_id: str = "999",
    parent_id: str = "222",
    guild_id: str = "111",
) -> dict[str, Any]:
    """Public-thread payload with ALL required keys for Thread._from_data."""
    return {
        "id": thread_id,
        "parent_id": parent_id,
        "owner_id": "1",
        "name": "test-thread",
        "type": 11,
        "message_count": 5,
        "member_count": 2,
        "thread_metadata": {
            "archived": False,
            "auto_archive_duration": 1440,
            "archive_timestamp": "2026-05-09T00:00:00+00:00",
        },
        "guild_id": guild_id,
    }


async def test_send_message_to_thread_succeeds_for_non_admin_when_parent_uncached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending to a thread must fetch the uncached parent channel and check
    permissions against it, not raise ClientException('Parent channel not
    found') from Thread.permissions_for on the REST-only client's empty
    channel cache."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL | _SEND_MESSAGES)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            if str(route.channel_id) == "999":
                return _thread_payload()
            if str(route.channel_id) == "222":
                return _text_channel_payload()
            raise AssertionError(f"unexpected channel fetch {route.channel_id}")
        if route.method == "POST" and route.path == "/channels/{channel_id}/messages":
            return _message_payload(message_id="9003", channel_id="999", content="in-thread")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    row = await _send_message_impl(
        _runtime_with_discord_token(),
        _auth(),
        channel_id="999",
        content="in-thread",
    )
    assert row.id == "9003", "send to thread should succeed once parent is hydrated"
    assert row.channel_id == "999", "message should land in the thread, not the parent"


async def test_send_message_to_thread_denied_when_parent_denies_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-admin without send_messages on the thread's parent gets ToolError,
    not ClientException, when the parent starts uncached."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]  # no send_messages
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            if str(route.channel_id) == "999":
                return _thread_payload()
            if str(route.channel_id) == "222":
                return _text_channel_payload()
            raise AssertionError(f"unexpected channel fetch {route.channel_id}")
        if route.method == "POST" and route.path == "/channels/{channel_id}/messages":
            raise AssertionError("must not POST when send permission denied")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    with pytest.raises(ToolError, match="send_messages"):
        await _send_message_impl(
            _runtime_with_discord_token(),
            _auth(),
            channel_id="999",
            content="x",
        )


# ---------------------------------------------------------------------------
# Regression pins for _require_discord_identity and _require_guild_id error
# strings. Exact-equality assertions so any future drift
# in the production wording — or accidental removal of the gate — fails CI
# loudly.
# ---------------------------------------------------------------------------

_EXPECTED_DISCORD_IDENTITY_ERROR = "discord tools require a discord-bound identity"
_EXPECTED_GUILD_CONTEXT_ERROR = "discord tools require a guild context"


async def test_require_discord_identity_raises_with_exact_error_string_when_platform_user_id_missing() -> (
    None
):
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="discord",
        external_id="111",
        platform_user_id=None,
    )
    with pytest.raises(ToolError) as exc_info:
        _require_discord_identity(auth)
    assert str(exc_info.value) == _EXPECTED_DISCORD_IDENTITY_ERROR, (
        "error message must remain stable for log-grep tooling and operator runbooks"
    )


async def test_require_discord_identity_raises_when_platform_is_not_discord() -> None:
    # CLI sessions carry platform="cli" claims with no
    # platform_user_id. The gate must continue to reject them with the same
    # message — the public check is `platform_user_id is None`.
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="cli",
        external_id=None,
        platform_user_id=None,
    )
    with pytest.raises(ToolError) as exc_info:
        _require_discord_identity(auth)
    assert str(exc_info.value) == _EXPECTED_DISCORD_IDENTITY_ERROR, (
        "non-discord callers must hit the same gate with the same message"
    )


async def test_require_guild_id_raises_with_exact_error_string_when_guild_id_missing() -> None:
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="discord",
        external_id=None,
        platform_user_id="42",
    )
    with pytest.raises(ToolError) as exc_info:
        _require_guild_id(auth)
    assert str(exc_info.value) == _EXPECTED_GUILD_CONTEXT_ERROR, (
        "error message must remain stable for log-grep tooling and operator runbooks"
    )
