"""Unit tests for the Discord display-identity implementation
(_set_display_identity_impl).

Each test calls the private impl directly with a hand-built ``AuthIdentity``
and a transport-level patched ``discord.http.HTTPClient``. Inline route
handlers and payload helpers per this file (no cross-test-file imports) so
SDK-payload drift breaks the relevant test.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import discord
import discord.http
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._identity import (
    _set_display_identity_impl,  # pyright: ignore[reportPrivateUsage]
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


# The fake ``static_login`` in conftest reports the bot as user "1".
_BOT_USER_ID = "1"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_CDN_URL = "https://cdn.discordapp.com/attachments/1/2/avatar.png"


# ---------------------------------------------------------------------------
# Tiny aiohttp fake (inline per guideline:testing; mirrors test_discord.py).
# ---------------------------------------------------------------------------


class _FakeAiohttpResponse:
    def __init__(self, *, chunks: list[bytes]) -> None:
        self.content_length = sum(len(c) for c in chunks)
        self._chunks = chunks
        self.content = self

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
        self.get_urls: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> _FakeAiohttpResponse:
        self.get_urls.append(url)
        return self._response


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


def _auth(*, is_admin: bool = True) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.ADMIN if is_admin else Role.USER,
        platform="discord",
        external_id="111",
        platform_user_id="42",
        is_admin=is_admin,
    )


def _guild_payload() -> dict[str, Any]:
    return {
        "id": "111",
        "name": "test-guild",
        "owner_id": "42",
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


def _member_payload(
    user_id: str, *, nick: str | None = None, avatar: str | None = None
) -> dict[str, Any]:
    return {
        "user": {
            "id": user_id,
            "username": "bot" if user_id == _BOT_USER_ID else "caller",
            "discriminator": "0001",
            "global_name": None,
            "avatar": None,
            "bot": user_id == _BOT_USER_ID,
            "flags": 0,
        },
        "nick": nick,
        "avatar": avatar,
        "roles": [],
        "joined_at": "2024-01-01T00:00:00+00:00",
        "deaf": False,
        "mute": False,
        "flags": 0,
    }


def _handler(
    *,
    captured_patch: dict[str, Any] | None = None,
    patch_error: Exception | None = None,
) -> Any:
    """Routes the impl's sequence: guild, bot member, then the self PATCH.
    Echoes the PATCH body back as the member."""

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            assert route.url.endswith(f"/members/{_BOT_USER_ID}"), (
                "only the bot's own member is fetched; the caller is vouched for by the JWT"
            )
            return _member_payload(_BOT_USER_ID)
        if route.method == "PATCH" and route.path == "/guilds/{guild_id}/members/@me":
            if patch_error is not None:
                raise patch_error
            if captured_patch is not None:
                captured_patch.update(kwargs)
            body = kwargs["json"]
            return _member_payload(
                _BOT_USER_ID,
                nick=body.get("nick"),
                avatar="hash123" if body.get("avatar") else None,
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    return handler


def _forbidden() -> discord.Forbidden:
    response = MagicMock()
    response.status = 403
    response.reason = "Forbidden"
    return discord.Forbidden(response, {"message": "Missing Permissions", "code": 50013})


def _bad_request() -> discord.HTTPException:
    response = MagicMock()
    response.status = 400
    response.reason = "Bad Request"
    return discord.HTTPException(response, {"message": "Invalid Form Body", "code": 50035})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_set_display_identity_sets_nickname_on_self(monkeypatch: pytest.MonkeyPatch) -> None:
    """The nickname goes to the self-member endpoint, stripped, and the row
    reflects the member Discord echoes back plus the server-wide hint."""
    captured: dict[str, Any] = {}
    patch_discord_http(monkeypatch, _handler(captured_patch=captured))

    row = await _set_display_identity_impl(
        _runtime_with_discord_token(), _auth(), display_name="  Daimon (open source)  "
    )

    assert captured["json"] == {"nick": "Daimon (open source)"}, (
        "the PATCH body must carry only the stripped nickname"
    )
    assert captured["reason"] == "set_display_identity requested by user 42", (
        "the audit log must record who asked for a server-wide change"
    )
    assert row.display_name == "Daimon (open source)", "row must carry the new nickname"
    assert row.guild_id == "111", "row must name the guild the change applied to"
    assert "whole server" in row.hint, "row must warn that the change is server-wide"


async def test_set_display_identity_uploads_attachment_as_avatar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Discord CDN image is fetched and sent as a base64 data URI; the
    nickname is left untouched when not given."""
    captured: dict[str, Any] = {}
    patch_discord_http(monkeypatch, _handler(captured_patch=captured))
    fake_session = _FakeAiohttpSession(_FakeAiohttpResponse(chunks=[_PNG_BYTES]))

    row = await _set_display_identity_impl(
        _runtime_with_discord_token(),
        _auth(),
        avatar_url=_CDN_URL,
        session=fake_session,  # type: ignore[arg-type]  # _FakeAiohttpSession satisfies the protocol
    )

    assert fake_session.get_urls == [_CDN_URL], "the avatar must be fetched from the given URL"
    assert "nick" not in captured["json"], "an omitted display_name must not touch the nickname"
    assert captured["json"]["avatar"].startswith("data:image/png;base64,"), (
        "the avatar must be sent as a data URI with the sniffed mime type"
    )
    assert row.avatar_url.endswith("hash123.png?size=1024"), "row must carry the new guild avatar"


async def test_set_display_identity_refuses_when_nothing_to_change() -> None:
    with pytest.raises(ToolError, match="display_name, avatar_url or both"):
        await _set_display_identity_impl(_runtime_with_discord_token(), _auth())


async def test_set_display_identity_refuses_long_nickname() -> None:
    with pytest.raises(ToolError, match="at most 32"):
        await _set_display_identity_impl(
            _runtime_with_discord_token(), _auth(), display_name="x" * 33
        )


async def test_set_display_identity_refuses_non_admin_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server-wide change is admin-only; the refusal happens before the
    REST client is even built."""

    async def no_requests(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        raise AssertionError(f"unexpected request {route.method} {route.path}")

    patch_discord_http(monkeypatch, no_requests)
    with pytest.raises(ToolError, match="admin"):
        await _set_display_identity_impl(
            _runtime_with_discord_token(), _auth(is_admin=False), display_name="Daimon"
        )


async def test_set_display_identity_refuses_non_cdn_avatar_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SSRF allowlist from send_message applies: only Discord CDN hosts."""
    patch_discord_http(monkeypatch, _handler())
    fake_session = _FakeAiohttpSession(_FakeAiohttpResponse(chunks=[_PNG_BYTES]))
    with pytest.raises(ToolError, match="discord cdn"):
        await _set_display_identity_impl(
            _runtime_with_discord_token(),
            _auth(),
            avatar_url="https://evil.example/avatar.png",
            session=fake_session,  # type: ignore[arg-type]
        )
    assert fake_session.get_urls == [], "a rejected URL must never be fetched"


async def test_set_display_identity_refuses_non_image_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    patch_discord_http(monkeypatch, _handler(captured_patch=captured))
    fake_session = _FakeAiohttpSession(_FakeAiohttpResponse(chunks=[b"not an image"]))
    with pytest.raises(ToolError, match="png, jpeg, gif or webp"):
        await _set_display_identity_impl(
            _runtime_with_discord_token(),
            _auth(),
            avatar_url=_CDN_URL,
            session=fake_session,  # type: ignore[arg-type]
        )
    assert captured == {}, "an unsupported image must be rejected before any PATCH"


async def test_set_display_identity_maps_forbidden_to_permission_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_discord_http(monkeypatch, _handler(patch_error=_forbidden()))
    with pytest.raises(ToolError, match="change_nickname"):
        await _set_display_identity_impl(
            _runtime_with_discord_token(), _auth(), display_name="Daimon"
        )


async def test_set_display_identity_sends_name_and_avatar_in_one_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    patch_discord_http(monkeypatch, _handler(captured_patch=captured))
    fake_session = _FakeAiohttpSession(_FakeAiohttpResponse(chunks=[_PNG_BYTES]))

    row = await _set_display_identity_impl(
        _runtime_with_discord_token(),
        _auth(),
        display_name="Daimon",
        avatar_url=_CDN_URL,
        session=fake_session,  # type: ignore[arg-type]
    )

    assert set(captured["json"]) == {"nick", "avatar"}, "both fields must land in one PATCH"
    assert row.display_name == "Daimon", "row must carry the new nickname"


async def test_set_display_identity_refuses_whitespace_only_nickname() -> None:
    with pytest.raises(ToolError, match="must not be empty"):
        await _set_display_identity_impl(_runtime_with_discord_token(), _auth(), display_name="   ")


async def test_set_display_identity_avatar_only_forbidden_does_not_blame_nickname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """change_nickname governs only the nick field; an avatar-only 403 must
    not send the admin off to grant an unrelated permission."""
    patch_discord_http(monkeypatch, _handler(patch_error=_forbidden()))
    fake_session = _FakeAiohttpSession(_FakeAiohttpResponse(chunks=[_PNG_BYTES]))
    with pytest.raises(ToolError, match="avatar") as excinfo:
        await _set_display_identity_impl(
            _runtime_with_discord_token(),
            _auth(),
            avatar_url=_CDN_URL,
            session=fake_session,  # type: ignore[arg-type]
        )
    assert "change_nickname" not in str(excinfo.value), (
        "an avatar-only refusal must not mention the nickname permission"
    )


async def test_set_display_identity_maps_http_error_to_discord_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_discord_http(monkeypatch, _handler(patch_error=_bad_request()))
    with pytest.raises(ToolError, match="discord refused the change: .*Invalid Form Body"):
        await _set_display_identity_impl(
            _runtime_with_discord_token(), _auth(), display_name="Daimon"
        )
