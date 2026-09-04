"""Unit tests for the GitHub App install-link MCP tool.

The install-URL builder's own unit test lives in core's tree
(``packages/core/tests/test_github_app_auth.py``); these tests cover the
tool and the posting path only. Discord REST calls are faked at transport
level via the sibling ``conftest.py``'s ``patch_discord_http`` (same trick
``test_credential_requests.py`` uses), never via method-level mocks. The
Slack posting path is covered in ``test_slack_app_install_button.py``.
"""

from __future__ import annotations

import importlib.util
import inspect
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import discord.http
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.github_app import (
    PostAppInstallLinkResult,
    _post_app_install_link_impl,  # pyright: ignore[reportPrivateUsage]
    register_github_app_tools,
)
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    GithubSettings,
    Settings,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import SecretStr

# Load patch_discord_http directly from the sibling conftest.py by file path —
# same trick test_credential_requests.py uses to dodge the "from conftest
# import ..." collision with the parent tests/conftest.py.
_conftest_path = Path(__file__).parent / "conftest.py"
_spec = importlib.util.spec_from_file_location("_tools_conftest", _conftest_path)
assert _spec is not None and _spec.loader is not None
_tools_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tools_conftest)
patch_discord_http = _tools_conftest.patch_discord_http

pytestmark = pytest.mark.asyncio

_VIEW_CHANNEL = 1 << 10
_SEND_MESSAGES = 1 << 11


# ---------------------------------------------------------------------------
# Helpers (mirrors test_credential_requests.py)
# ---------------------------------------------------------------------------


def _runtime(*, with_discord: bool = True, app_slug: str | None = "acme-daimon") -> McpRuntime:
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://x/y"),  # pyright: ignore[reportArgumentType]
        anthropic=AnthropicSettings(api_key=SecretStr("k")),
        discord=DiscordSettings(bot_token=SecretStr("test-bot-token")) if with_discord else None,
        github=GithubSettings(app_slug=app_slug),
    )
    return McpRuntime(
        session_factory=MagicMock(),  # type: ignore[arg-type]
        client=MagicMock(),  # type: ignore[arg-type]
        settings=settings,
        deployment_default=DeploymentDefault(),
    )


def _auth_identity(
    *,
    platform: str | None = "discord",
    external_id: str | None = "111",
    platform_user_id: str | None = "42",
    is_admin: bool = False,
) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform=platform,
        external_id=external_id,
        platform_user_id=platform_user_id,
        is_admin=is_admin,
    )


def _guild_payload(*, guild_id: str = "111") -> dict[str, Any]:
    return {
        "id": guild_id,
        "name": "test-guild",
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


def _text_channel_payload(*, channel_id: str = "222", guild_id: str = "111") -> dict[str, Any]:
    return {
        "id": channel_id,
        "type": 0,
        "guild_id": guild_id,
        "name": "general",
        "position": 0,
        "permission_overwrites": [],
        "nsfw": False,
        "rate_limit_per_user": 0,
        "parent_id": None,
    }


def _message_payload(*, message_id: str, channel_id: str = "222") -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": channel_id,
        "author": {
            "id": "1",
            "username": "bot",
            "discriminator": "0001",
            "global_name": "bot",
            "avatar": None,
            "bot": True,
            "flags": 0,
        },
        "content": "x",
        "timestamp": "2026-05-09T00:00:00+00:00",
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


def _patch_successful_post(
    monkeypatch: pytest.MonkeyPatch,
    *,
    message_id: str,
    posted: dict[str, Any] | None = None,
    can_send: bool = True,
) -> None:
    perms = (_VIEW_CHANNEL | _SEND_MESSAGES) if can_send else _VIEW_CHANNEL

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", perms)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload()
        if route.method == "POST" and route.path == "/channels/{channel_id}/messages":
            if posted is not None:
                posted.update(kwargs)
            return _message_payload(message_id=message_id)
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)


def _outbound_request_count(monkeypatch: pytest.MonkeyPatch) -> list[discord.http.Route]:
    captured: list[discord.http.Route] = []

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        captured.append(route)
        raise AssertionError(f"unexpected outbound request {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)
    return captured


# ---------------------------------------------------------------------------
# 1. Happy path: posts one message with exactly one link-style button, no
#    custom id
# ---------------------------------------------------------------------------


async def test_post_app_install_link_posts_link_button_with_no_custom_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(app_slug="acme-daimon")
    auth = _auth_identity()
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9301", posted=posted)

    result = await _post_app_install_link_impl(
        runtime, auth, channel_id="222", purpose="reading a private repo"
    )

    assert result.message_id == "9301", "result must report the posted message id"
    assert result.channel_id == "222", "result must report the target channel id"
    assert result.install_url == "https://github.com/apps/acme-daimon/installations/new", (
        "result must report the exact install URL"
    )

    components = posted["json"]["components"]
    assert len(components) == 1, "exactly one action row must be posted"
    buttons = components[0]["components"]
    assert len(buttons) == 1, "exactly one button must be posted"
    button = buttons[0]
    assert button["style"] == discord.ButtonStyle.link.value, "button must use the link style"
    assert button["url"] == "https://github.com/apps/acme-daimon/installations/new", (
        "button url must be the built install URL"
    )
    assert "custom_id" not in button, (
        "a link button must carry no custom_id — nothing dispatches this button"
    )


# ---------------------------------------------------------------------------
# 2. Result model carries no success-shaped field
# ---------------------------------------------------------------------------


async def test_result_model_has_no_success_shaped_field() -> None:
    forbidden_words = ("installed", "success", "completed", "connected", "verified")
    field_names = set(PostAppInstallLinkResult.model_fields)
    offending = {
        name for name in field_names if any(word in name.lower() for word in forbidden_words)
    }
    assert not offending, f"result model must carry no success-shaped field, found {offending}"


# ---------------------------------------------------------------------------
# 4. Caller with no platform-bound identity is rejected
# ---------------------------------------------------------------------------


async def test_post_app_install_link_rejects_missing_platform_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    auth = _auth_identity(platform_user_id=None)
    captured = _outbound_request_count(monkeypatch)

    with pytest.raises(ToolError, match="platform-bound identity"):
        await _post_app_install_link_impl(
            runtime, auth, channel_id="222", purpose="reading a private repo"
        )
    assert captured == [], "a rejected call with no platform-bound identity must post nothing"


# ---------------------------------------------------------------------------
# 5. Unset slug is rejected, posts nothing
# ---------------------------------------------------------------------------


async def test_post_app_install_link_rejects_unset_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(app_slug=None)
    auth = _auth_identity()
    captured = _outbound_request_count(monkeypatch)

    with pytest.raises(ToolError, match="no GitHub App install link configured"):
        await _post_app_install_link_impl(
            runtime, auth, channel_id="222", purpose="reading a private repo"
        )
    assert captured == [], "an unset slug must post zero outbound Discord requests"


# ---------------------------------------------------------------------------
# 6. Caller lacking send permission is refused by the reused permission check
# ---------------------------------------------------------------------------


async def test_post_app_install_link_rejects_caller_without_send_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    auth = _auth_identity()
    _patch_successful_post(monkeypatch, message_id="9302", can_send=False)

    with pytest.raises(ToolError, match="missing send_messages permission"):
        await _post_app_install_link_impl(
            runtime, auth, channel_id="222", purpose="reading a private repo"
        )


# ---------------------------------------------------------------------------
# 7. Tool registration
# ---------------------------------------------------------------------------


async def test_post_github_app_install_link_is_registered() -> None:
    mcp = FastMCP(name="test")
    register_github_app_tools(mcp, _runtime())
    tools = await mcp.list_tools()
    by_name = {t.name: t for t in tools}
    assert "post_github_app_install_link" in by_name, "the tool must be registered"


async def test_impl_signature_has_no_token_secret_or_value_parameter() -> None:
    params = set(inspect.signature(_post_app_install_link_impl).parameters)
    forbidden = {p for p in params if any(w in p.lower() for w in ("token", "secret", "value"))}
    assert not forbidden, (
        f"_post_app_install_link_impl must not accept a secret-bearing parameter, found {forbidden}"
    )
