"""DB-backed unit tests for the credential-request MCP tools.

Each test calls the private ``_*_impl`` functions directly with a real
sessionmaker (real Postgres), a transport-level Anthropic fake (``MARouter``)
for the agent-name resolution, and a transport-level patched Discord
``HTTPClient`` (``patch_discord_http``, loaded from the sibling conftest.py by
file path — see test_discord.py for the same pattern) for the button post.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import daimon.adapters.mcp.tools.credential_requests as _credential_requests_mod
import discord.http
import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
)
from daimon.core.continuity.continuation import MAX_REQUESTED_WORK
from daimon.core.credential_requests import (
    CUSTOM_ID_PREFIX,
    DEFAULT_TTL,
    ENV_FILE_TARGET,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.posted_controls import REPLACED_HEADLINE
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.stores.credential_requests import peek_credential_request
from daimon.core.stores.domain import Role
from daimon.core.stores.turn_origins import create_origin
from daimon.testing import ma_agent, ma_model_config
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Load patch_discord_http directly from the sibling conftest.py by file path —
# same trick test_discord.py uses to dodge the "from conftest import ..."
# collision with the parent tests/conftest.py.
_conftest_path = Path(__file__).parent / "conftest.py"
_spec = importlib.util.spec_from_file_location("_tools_conftest", _conftest_path)
assert _spec is not None and _spec.loader is not None
_tools_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tools_conftest)
patch_discord_http = _tools_conftest.patch_discord_http

_request_agent_key_impl = (
    _credential_requests_mod._request_agent_key_impl  # pyright: ignore[reportPrivateUsage]
)
_request_mcp_token_impl = (
    _credential_requests_mod._request_mcp_token_impl  # pyright: ignore[reportPrivateUsage]
)
_request_mcp_oauth_impl = (
    _credential_requests_mod._request_mcp_oauth_impl  # pyright: ignore[reportPrivateUsage]
)
_request_repo_binding_impl = (
    _credential_requests_mod._request_repo_binding_impl  # pyright: ignore[reportPrivateUsage]
)
register_credential_request_tools = _credential_requests_mod.register_credential_request_tools


_VIEW_CHANNEL = 1 << 10
_SEND_MESSAGES = 1 << 11


# ---------------------------------------------------------------------------
# Helpers (small, intentional — mirrors test_routines.py / test_discord.py)
# ---------------------------------------------------------------------------


def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    client: AsyncAnthropic | None = None,
    with_discord: bool = True,
    deployment_default: DeploymentDefault | None = None,
) -> McpRuntime:
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://x/y"),  # pyright: ignore[reportArgumentType]
        anthropic=AnthropicSettings(api_key=SecretStr("k")),
        discord=DiscordSettings(bot_token=SecretStr("test-bot-token")) if with_discord else None,
    )
    return McpRuntime(
        session_factory=sessionmaker,
        client=client if client is not None else MagicMock(),  # type: ignore[arg-type]
        settings=settings,
        deployment_default=(
            deployment_default if deployment_default is not None else DeploymentDefault()
        ),
    )


def _auth_identity(
    *,
    platform: str | None = "discord",
    external_id: str | None = "111",
    platform_user_id: str | None = "42",
    tenant_id: uuid.UUID | None = None,
    is_admin: bool = False,
) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id if tenant_id is not None else uuid.uuid4(),
        role=Role.USER,
        platform=platform,
        external_id=external_id,
        platform_user_id=platform_user_id,
        is_admin=is_admin,
    )


def _ma_agent(
    *, agent_id: str, name: str, tenant_id: uuid.UUID, managed: bool = False
) -> dict[str, object]:
    metadata = {
        MA_METADATA_KEY_TENANT: str(tenant_id),
        MA_METADATA_KEY_NAME: name,
    }
    if managed:
        metadata[MA_METADATA_KEY_MANAGED] = "true"
    agent = ma_agent(
        id=agent_id,
        name=name,
        model=ma_model_config("claude-sonnet-4-6", speed="standard"),
        metadata=metadata,
    )
    return agent.model_dump(mode="json")


def _ma_client_with_agents(agents: list[dict[str, object]]) -> AsyncAnthropic:
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response(agents))
    return build_fake_anthropic(router.dispatch)


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


def _message_payload(
    *, message_id: str, channel_id: str = "222", content: str = "x"
) -> dict[str, Any]:
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
        "content": content,
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
    monkeypatch: pytest.MonkeyPatch, *, message_id: str, posted: dict[str, Any] | None = None
) -> None:
    """Patch a successful button post. When ``posted`` is given, the POSTed
    kwargs are recorded into it so the caller can pull the minted token back
    out of the button's custom_id (``ztc:{token}``) for a store-level
    ``peek_credential_request`` field check — never via a raw ORM import."""

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL | _SEND_MESSAGES)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload()
        if route.method == "POST" and route.path == "/channels/{channel_id}/messages":
            if posted is not None:
                posted.update(kwargs)
            return _message_payload(message_id=message_id)
        if route.method == "PATCH" and route.path == "/channels/{channel_id}/messages/{message_id}":
            # A second mint in the same thread retires the first card by
            # editing it; a test that is not about that edit just lets it
            # through. Use `_patch_post_and_edit` to assert on one.
            return _message_payload(message_id=message_id)
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)


def _patch_post_and_edit(
    monkeypatch: pytest.MonkeyPatch,
    *,
    message_ids: list[str],
    posted: list[dict[str, Any]],
    edited: dict[str, dict[str, Any]],
) -> None:
    """Patch a transport that can post several cards and edit an earlier one.

    `_patch_successful_post` serves exactly one POST and refuses every other
    route; a supersede posts the new card and then PATCHes the one it retired,
    so both are served here. POSTs take their ids from ``message_ids`` in
    order and land in ``posted``; each PATCH lands in ``edited`` under the
    message id it targeted.
    """
    ids = iter(message_ids)

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL | _SEND_MESSAGES)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload()
        if route.method == "POST" and route.path == "/channels/{channel_id}/messages":
            posted.append(kwargs)
            return _message_payload(message_id=next(ids))
        if route.method == "PATCH" and route.path == "/channels/{channel_id}/messages/{message_id}":
            message_id = route.url.rsplit("/", 1)[-1]
            edited[message_id] = kwargs
            return _message_payload(message_id=message_id)
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)


def _walk_components(components: Any) -> Iterator[dict[str, Any]]:
    """Yield every component dict in a components-v2 payload, depth first.

    The card is a nested container (`type: 17`) holding text displays and an
    action row, so the button no longer sits at a fixed index — walking the
    tree keeps these tests reading the payload rather than its shape.
    """
    if isinstance(components, dict):
        yield components  # pyright: ignore[reportUnknownArgumentType]
        for value in components.values():  # pyright: ignore[reportUnknownVariableType]
            if isinstance(value, (dict, list)):
                yield from _walk_components(value)
    elif isinstance(components, list):
        for item in components:  # pyright: ignore[reportUnknownVariableType]
            yield from _walk_components(item)


def _button_from_posted(posted: dict[str, Any]) -> dict[str, Any]:
    buttons = [
        c
        for c in _walk_components(posted["json"]["components"])
        if str(c.get("custom_id", "")).startswith(CUSTOM_ID_PREFIX)
    ]
    assert len(buttons) == 1, f"expected exactly one credential button, got {len(buttons)}"
    return buttons[0]


def _card_text(posted: dict[str, Any]) -> str:
    """Every rendered text run in the posted card, joined."""
    return "\n".join(
        str(c["content"]) for c in _walk_components(posted["json"]["components"]) if "content" in c
    )


def _token_from_posted(posted: dict[str, Any]) -> str:
    custom_id: str = _button_from_posted(posted)["custom_id"]
    return custom_id[len(CUSTOM_ID_PREFIX) :]


async def _row_count(db_session: AsyncSession) -> int:
    result = await db_session.execute(text("SELECT COUNT(*) FROM credential_requests"))
    return result.scalar_one()


# ---------------------------------------------------------------------------
# 1. request_agent_key creates a row + posts a button
# ---------------------------------------------------------------------------


async def test_request_agent_key_creates_row_and_posts_button(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_env", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    async with committing_sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=auth.account_id,
            platform=auth.platform or "discord",
            parent_channel_id="C_CRED" if auth.platform == "slack" else "1111",
            thread_id="1700000000.000001" if auth.platform == "slack" else "222",
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )
    before = datetime.now(UTC)
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9201", posted=posted)

    result = await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin.id),
        expected_ma_agent_id="ag_env",
        agent_name="daimon",
        key="OPENAI_API_KEY",
        purpose="calling the OpenAI API",
        channel_id="999-untrusted",
    )

    assert result.kind == "env", "result must report the env kind"
    assert result.target == "OPENAI_API_KEY", "result must report the exact key"
    assert result.message_id == "9201", "result must report the posted message id"
    assert await _row_count(db_session) == 1, "exactly one credential_requests row must be created"

    row = await peek_credential_request(db_session, token=_token_from_posted(posted))
    assert row is not None, "the minted token must resolve to the created row"
    assert before + DEFAULT_TTL <= row.expires_at, "the row must expire no sooner than now + TTL"
    assert row.kind == "env"
    assert row.mcp_server_url is None, "env rows must not carry an mcp_server_url"
    assert row.account_id == auth.account_id, "row must stamp the caller's account_id"
    assert row.requester_platform_user_id == auth.platform_user_id, (
        "row must stamp the caller's platform_user_id"
    )
    assert row.tenant_id == tenant.id

    assert row.platform == "discord", "request retains platform after the turn ends"
    assert row.channel_id == "222" and row.origin_thread_id == "222", "origin controls destination"
    assert row.parent_channel_id == "1111", "parent and thread remain separately identifiable"
    assert row.posted_message_id == "9201", "durable outcomes target the actual card"


# ---------------------------------------------------------------------------
# 2. request_mcp_token creates a row + posts a button
# ---------------------------------------------------------------------------


async def test_request_mcp_token_creates_row_and_posts_button(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_mcp", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    async with committing_sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=auth.account_id,
            platform=auth.platform or "discord",
            parent_channel_id="C_CRED" if auth.platform == "slack" else "1111",
            thread_id="1700000000.000001" if auth.platform == "slack" else "222",
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9202", posted=posted)

    result = await _request_mcp_token_impl(
        runtime,
        auth,
        origin_context_id=str(origin.id),
        expected_ma_agent_id="ag_mcp",
        agent_name="daimon",
        server_name="linear",
        url="https://mcp.linear.app/sse",
        channel_id="222",
    )

    assert result.kind == "mcp"
    assert result.target == "linear"
    assert result.message_id == "9202"
    assert await _row_count(db_session) == 1, "exactly one credential_requests row must be created"

    row = await peek_credential_request(db_session, token=_token_from_posted(posted))
    assert row is not None, "the minted token must resolve to the created row"
    assert row.kind == "mcp"
    assert row.mcp_server_url == "https://mcp.linear.app/sse", "mcp rows must carry the server url"


async def test_request_mcp_oauth_creates_an_oauth_row_and_posts_the_connect_card(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_mcp", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    async with committing_sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=auth.account_id,
            platform="discord",
            parent_channel_id="1111",
            thread_id="222",
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9303", posted=posted)

    result = await _request_mcp_oauth_impl(
        runtime,
        auth,
        origin_context_id=str(origin.id),
        expected_ma_agent_id="ag_mcp",
        agent_name="daimon",
        server_name="notion",
        url="https://mcp.notion.com/mcp/",
        channel_id="222",
    )

    assert result.kind == "mcp_oauth" and result.target == "notion"
    row = await peek_credential_request(db_session, token=_token_from_posted(posted))
    assert row is not None and row.kind == "mcp_oauth"
    assert row.mcp_server_url == "https://mcp.notion.com/mcp", "the url is stored without its slash"
    assert "Connect my account" in json.dumps(posted, default=str), (
        "the card offers a sign-in, not a token form"
    )


async def test_request_mcp_oauth_refuses_a_private_address() -> None:
    runtime = _runtime(MagicMock())
    with pytest.raises(ToolError, match="non-public address"):
        await _request_mcp_oauth_impl(
            runtime,
            _auth_identity(),
            agent_name="daimon",
            server_name="metadata",
            url="https://169.254.169.254/computeMetadata/v1/",
            channel_id="222",
        )


async def test_request_mcp_token_refuses_a_private_address(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """The token form probes the URL from inside the deployment: no internal hosts."""
    runtime = _runtime(committing_sessionmaker)
    with pytest.raises(ToolError, match="non-public address"):
        await _request_mcp_token_impl(
            runtime,
            _auth_identity(),
            agent_name="daimon",
            server_name="redis",
            url="http://10.0.0.5:6379/",
            channel_id="222",
        )
    assert await _row_count(db_session) == 0


async def test_request_mcp_oauth_refuses_the_reserved_daimon_mcp_entry() -> None:
    """A grant at the deployment's own entry would take the slot the per-agent
    JWT needs and 409 every session create; same gate as attach_mcp_server."""
    runtime = _runtime(MagicMock())
    with pytest.raises(ToolError, match="daimon-mcp"):
        await _request_mcp_oauth_impl(
            runtime,
            _auth_identity(),
            agent_name="daimon",
            server_name="daimon-mcp",
            url="https://mcp.notion.com/mcp",
            channel_id="222",
        )


async def test_request_mcp_token_refuses_the_reserved_daimon_mcp_entry(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    runtime = _runtime(committing_sessionmaker)
    with pytest.raises(ToolError, match="daimon-mcp"):
        await _request_mcp_token_impl(
            runtime,
            _auth_identity(),
            agent_name="daimon",
            server_name="daimon-mcp",
            url="https://mcp.linear.app/sse",
            channel_id="222",
        )
    assert await _row_count(db_session) == 0


async def test_request_mcp_oauth_refuses_a_plain_http_server() -> None:
    runtime = _runtime(MagicMock())
    with pytest.raises(ToolError, match="https"):
        await _request_mcp_oauth_impl(
            runtime,
            _auth_identity(),
            agent_name="daimon",
            server_name="notion",
            url="http://mcp.notion.com/mcp",
            channel_id="222",
        )


# ---------------------------------------------------------------------------
# 3. Neither registered tool exposes a token/secret/value parameter
# ---------------------------------------------------------------------------


async def test_neither_tool_signature_exposes_a_secret_bearing_parameter() -> None:
    mcp = FastMCP(name="test")
    register_credential_request_tools(mcp, _runtime(MagicMock()))  # type: ignore[arg-type]
    tools = await mcp.list_tools()
    by_name = {t.name: t for t in tools}
    assert {"request_agent_key", "request_mcp_token"} <= by_name.keys(), (
        "both tools must be registered"
    )
    for name in ("request_agent_key", "request_mcp_token"):
        param_names = set(by_name[name].parameters.get("properties", {}))
        forbidden = {
            p for p in param_names if any(w in p.lower() for w in ("token", "secret", "value"))
        }
        assert not forbidden, (
            f"{name} must not expose a secret-bearing parameter, found {forbidden}"
        )


async def test_impl_signatures_have_no_token_secret_or_value_parameter() -> None:
    """Belt-and-suspenders: the underlying impls' own signatures, not just the
    FastMCP-registered schema, carry no token/secret/value parameter."""
    for fn in (_request_agent_key_impl, _request_mcp_token_impl):
        params = set(inspect.signature(fn).parameters)
        forbidden = {p for p in params if any(w in p.lower() for w in ("token", "secret", "value"))}
        assert not forbidden, (
            f"{fn.__name__} must not accept a secret-bearing parameter, found {forbidden}"
        )


# ---------------------------------------------------------------------------
# 4. A non-admin caller succeeds — there is no admin gate
# ---------------------------------------------------------------------------


async def test_request_agent_key_succeeds_for_non_admin_caller(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_nonadmin", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id, is_admin=False)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    async with committing_sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=auth.account_id,
            platform=auth.platform or "discord",
            parent_channel_id="C_CRED" if auth.platform == "slack" else "1111",
            thread_id="1700000000.000001" if auth.platform == "slack" else "222",
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )
    _patch_successful_post(monkeypatch, message_id="9203")

    result = await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin.id),
        expected_ma_agent_id="ag_nonadmin",
        agent_name="daimon",
        key="TOGGL_TOKEN",
        purpose="tracking time",
        channel_id="222",
    )
    assert result.target == "TOGGL_TOKEN", (
        "a non-admin caller must succeed — there is no admin gate"
    )


# ---------------------------------------------------------------------------
# 5. A Slack caller gets a ToolError; no row created
# ---------------------------------------------------------------------------


async def test_request_agent_key_rejects_slack_caller_without_workspace(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(platform="slack", external_id=None, platform_user_id="U123")
    with pytest.raises(ToolError, match="workspace context"):
        await _request_agent_key_impl(
            runtime, auth, agent_name="daimon", key="OPENAI_API_KEY", purpose="x", channel_id="222"
        )
    assert await _row_count(db_session) == 0, "a rejected slack call must create no row"


async def test_request_mcp_token_rejects_slack_caller_without_workspace(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(platform="slack", external_id=None, platform_user_id="U123")
    with pytest.raises(ToolError, match="workspace context"):
        await _request_mcp_token_impl(
            runtime,
            auth,
            agent_name="daimon",
            server_name="linear",
            url="https://mcp.linear.app/sse",
            channel_id="222",
        )
    assert await _row_count(db_session) == 0, "a rejected slack call must create no row"


# ---------------------------------------------------------------------------
# 6. A caller with no platform_user_id gets a ToolError; no row created
# ---------------------------------------------------------------------------


async def test_request_agent_key_rejects_missing_platform_user_id(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(platform_user_id=None)
    with pytest.raises(ToolError, match="platform-bound identity"):
        await _request_agent_key_impl(
            runtime, auth, agent_name="daimon", key="OPENAI_API_KEY", purpose="x", channel_id="222"
        )
    assert await _row_count(db_session) == 0


# ---------------------------------------------------------------------------
# 7. An env key failing the POSIX rule gets a ToolError; no row created
# ---------------------------------------------------------------------------


async def test_request_agent_key_rejects_invalid_key(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()
    with pytest.raises(ToolError, match=r"\[A-Za-z_\]\[A-Za-z0-9_\]\*"):
        await _request_agent_key_impl(
            runtime, auth, agent_name="daimon", key="1BAD-KEY", purpose="x", channel_id="222"
        )
    assert await _row_count(db_session) == 0


async def test_request_agent_key_rejects_a_tool_control_name_for_everyone(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """TAR_OPTIONS is hard-denied: even an admin cannot store it as a key."""
    runtime = _runtime(committing_sessionmaker)
    admin = _auth_identity(is_admin=True)
    with pytest.raises(ToolError, match="reserved name"):
        await _request_agent_key_impl(
            runtime, admin, agent_name="daimon", key="TAR_OPTIONS", purpose="x", channel_id="222"
        )
    assert await _row_count(db_session) == 0


async def test_request_agent_key_member_allowlist_but_admin_may_add_any_name(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-admin may add a credential name, not a free-form one; an admin may add either."""
    runtime = _runtime(committing_sessionmaker)
    member = _auth_identity()
    # A member cannot add a non-credential name (no mint, no row).
    with pytest.raises(ToolError, match="not a secret name a member can add"):
        await _request_agent_key_impl(
            runtime, member, agent_name="daimon", key="DATABASE_URL", purpose="x", channel_id="222"
        )
    assert await _row_count(db_session) == 0

    # An admin may: this mints a row (full happy path needs an agent + origin).
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_env", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    admin = _auth_identity(tenant_id=tenant.id, is_admin=True)
    await make_account(db_session, tenant=tenant, id=admin.account_id)
    await db_session.commit()
    async with committing_sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=admin.account_id,
            platform="discord",
            parent_channel_id="1111",
            thread_id="222",
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=admin.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )
    _patch_successful_post(monkeypatch, message_id="9301", posted={})
    result = await _request_agent_key_impl(
        runtime,
        admin,
        origin_context_id=str(origin.id),
        expected_ma_agent_id="ag_env",
        agent_name="daimon",
        key="DATABASE_URL",
        purpose="connection string",
        channel_id="999",
    )
    assert result.target == "DATABASE_URL", "an admin may add a non-credential name"
    assert await _row_count(db_session) == 1


@pytest.mark.parametrize(
    "key", ["LD_PRELOAD", "BASH_ENV", "PATH", "GIT_CONFIG_KEY_0", "HTTPS_PROXY", "NODE_OPTIONS"]
)
async def test_request_agent_key_rejects_a_reserved_key(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    key: str,
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()
    with pytest.raises(ToolError, match="reserved name"):
        await _request_agent_key_impl(
            runtime, auth, agent_name="daimon", key=key, purpose="x", channel_id="222"
        )
    assert await _row_count(db_session) == 0


# ---------------------------------------------------------------------------
# 8. An agent_name with no matching MA agent gets a ToolError; no row created
# ---------------------------------------------------------------------------


async def test_request_agent_key_rejects_unknown_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents([])  # no agents in the tenant
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    async with committing_sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=auth.account_id,
            platform=auth.platform or "discord",
            parent_channel_id="C_CRED" if auth.platform == "slack" else "1111",
            thread_id="1700000000.000001" if auth.platform == "slack" else "222",
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )
    with pytest.raises(ToolError, match="missing"):
        await _request_agent_key_impl(
            runtime,
            auth,
            origin_context_id=str(origin.id),
            expected_ma_agent_id="ag_missing",
            agent_name="ghost",
            key="OPENAI_API_KEY",
            purpose="x",
            channel_id="222",
        )
    assert await _row_count(db_session) == 0


# ---------------------------------------------------------------------------
# 9. An mcp url that is not http(s) gets a ToolError; no row created
# ---------------------------------------------------------------------------


async def test_request_mcp_token_rejects_non_http_url(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()
    with pytest.raises(ToolError, match="must be http or https"):
        await _request_mcp_token_impl(
            runtime,
            auth,
            agent_name="daimon",
            server_name="linear",
            url="ftp://mcp.linear.app/sse",
            channel_id="222",
        )
    assert await _row_count(db_session) == 0


# ---------------------------------------------------------------------------
# 10. A failed button post raises a ToolError rather than silently succeeding
# ---------------------------------------------------------------------------


async def test_request_repo_binding_creates_row_and_posts_button(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_repo", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    async with committing_sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=auth.account_id,
            platform=auth.platform or "discord",
            parent_channel_id="C_CRED" if auth.platform == "slack" else "1111",
            thread_id="1700000000.000001" if auth.platform == "slack" else "222",
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )
    before = datetime.now(UTC)
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9204", posted=posted)

    result = await _request_repo_binding_impl(
        runtime,
        auth,
        origin_context_id=str(origin.id),
        expected_ma_agent_id="ag_repo",
        agent_name="daimon",
        repo_url="https://github.com/clsandoval/daimon-qa-scratch",
        purpose="binding the QA scratch repo",
        channel_id="222",
    )

    assert result.kind == "repo", "result must report the repo kind"
    assert result.target == "https://github.com/clsandoval/daimon-qa-scratch@main", (
        "result must report the repo url packed with the default branch"
    )
    assert result.message_id == "9204", "result must report the posted message id"
    assert await _row_count(db_session) == 1, "exactly one credential_requests row must be created"

    row = await peek_credential_request(db_session, token=_token_from_posted(posted))
    assert row is not None, "the minted token must resolve to the created row"
    assert before + DEFAULT_TTL <= row.expires_at, "the row must expire no sooner than now + TTL"
    assert row.kind == "repo"
    assert row.target == "https://github.com/clsandoval/daimon-qa-scratch@main", (
        "the row stores the branch packed into the target"
    )
    assert row.mcp_server_url is None, "repo rows must not carry an mcp_server_url"
    assert row.account_id == auth.account_id, "row must stamp the caller's account_id"
    assert row.requester_platform_user_id == auth.platform_user_id, (
        "row must stamp the caller's platform_user_id"
    )
    assert row.tenant_id == tenant.id


async def test_request_repo_binding_rejects_unknown_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents([])  # no agents in the tenant
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    async with committing_sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=auth.account_id,
            platform=auth.platform or "discord",
            parent_channel_id="C_CRED" if auth.platform == "slack" else "1111",
            thread_id="1700000000.000001" if auth.platform == "slack" else "222",
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )
    with pytest.raises(ToolError, match="missing"):
        await _request_repo_binding_impl(
            runtime,
            auth,
            origin_context_id=str(origin.id),
            expected_ma_agent_id="ag_missing",
            agent_name="ghost",
            repo_url="https://github.com/clsandoval/daimon-qa-scratch",
            purpose="x",
            channel_id="222",
        )
    assert await _row_count(db_session) == 0, "an unknown agent must create no row"


async def test_request_repo_binding_rejects_invalid_repo_url(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()
    with pytest.raises(ToolError, match="owner/repo"):
        await _request_repo_binding_impl(
            runtime,
            auth,
            agent_name="daimon",
            repo_url="https://github.com/not-a-repo-shape",
            purpose="x",
            channel_id="222",
        )
    assert await _row_count(db_session) == 0, "an invalid repo url must create no row"


async def test_request_repo_binding_signature_has_no_secret_or_branch_parameter() -> None:
    """Belt-and-suspenders: the repo-binding impl's own signature carries no
    token/secret/value/pat parameter, and no default_branch parameter — the
    row has no column to persist one."""
    params = set(inspect.signature(_request_repo_binding_impl).parameters)
    forbidden = {
        p
        for p in params
        if any(w in p.lower() for w in ("token", "secret", "value", "pat", "default_branch"))
    }
    assert not forbidden, (
        f"_request_repo_binding_impl must not accept a secret or branch parameter, "
        f"found {forbidden}"
    )


async def test_request_repo_binding_posts_message_naming_agent_and_repo(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_repo_msg", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    async with committing_sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=auth.account_id,
            platform=auth.platform or "discord",
            parent_channel_id="C_CRED" if auth.platform == "slack" else "1111",
            thread_id="1700000000.000001" if auth.platform == "slack" else "222",
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9205", posted=posted)

    await _request_repo_binding_impl(
        runtime,
        auth,
        origin_context_id=str(origin.id),
        expected_ma_agent_id="ag_repo_msg",
        agent_name="daimon",
        repo_url="https://github.com/clsandoval/daimon-qa-scratch",
        purpose="binding the QA scratch repo",
        channel_id="222",
    )

    card_text = _card_text(posted)
    assert "daimon" in card_text, "the card must name the agent"
    assert "clsandoval/daimon-qa-scratch" in card_text, "the card must name the exact repo"
    button = _button_from_posted(posted)
    assert button["custom_id"].startswith(CUSTOM_ID_PREFIX), (
        "the button's custom_id must carry the credential-request prefix"
    )


async def test_request_agent_key_raises_when_button_post_fails(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_fail", name="daimon", tenant_id=tenant.id)]
    )
    # No discord settings configured -> _post_credential_button_impl's
    # _require_bot_token raises inside the post step, after the row is
    # already minted.
    runtime = _runtime(committing_sessionmaker, client=client, with_discord=False)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    async with committing_sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=auth.account_id,
            platform=auth.platform or "discord",
            parent_channel_id="C_CRED" if auth.platform == "slack" else "1111",
            thread_id="1700000000.000001" if auth.platform == "slack" else "222",
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )

    with pytest.raises(ToolError, match="posting the button failed"):
        await _request_agent_key_impl(
            runtime,
            auth,
            origin_context_id=str(origin.id),
            expected_ma_agent_id="ag_fail",
            agent_name="daimon",
            key="OPENAI_API_KEY",
            purpose="x",
            channel_id="222",
        )

    # The row is minted before the post is attempted — a failed post leaves it
    # in place (single-use + TTL bound it regardless), it just never got a
    # live button. This documents that reality rather than asserting it away.
    assert await _row_count(db_session) == 1, (
        "the credential request row is created before the button post is attempted"
    )


# ---------------------------------------------------------------------------
# Slack callers: the same mint, a Slack button post
# ---------------------------------------------------------------------------

_SLACK_TEAM_ID = "T_TEST"
_SLACK_USER_ID = "U_CALLER"


def _slack_runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    client: AsyncAnthropic | None = None,
    fernet: Any = None,
) -> McpRuntime:
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://x/y"),  # pyright: ignore[reportArgumentType]
        anthropic=AnthropicSettings(api_key=SecretStr("k")),
    )
    return McpRuntime(
        session_factory=sessionmaker,
        client=client if client is not None else MagicMock(),  # type: ignore[arg-type]
        settings=settings,
        deployment_default=DeploymentDefault(),
        fernet=fernet,
    )


async def _seed_slack_bot_token(sessionmaker: async_sessionmaker[AsyncSession]) -> Any:
    from cryptography.fernet import Fernet
    from daimon.core.github_credentials import build_multifernet, encrypt_token
    from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token

    fernet = build_multifernet((Fernet.generate_key().decode("ascii"),))
    async with sessionmaker() as session:
        await upsert_slack_bot_token(
            session,
            team_id=_SLACK_TEAM_ID,
            encrypted_token=encrypt_token(fernet, "xoxb-secret"),
        )
        await session.commit()
    return fernet


def _register_slack_post_defaults(m: Any, *, channel_id: str = "C_CRED") -> None:
    import re as _re

    channel_payload = {
        "ok": True,
        "channel": {"id": channel_id, "is_private": False, "is_im": False, "is_mpim": False},
    }
    m.post("https://slack.com/api/conversations.info", payload=channel_payload, repeat=True)
    m.get(
        _re.compile(r"https://slack\.com/api/conversations\.info.*"),
        payload=channel_payload,
        repeat=True,
    )
    m.post(
        _re.compile(r"https://slack\.com/api/users\.info.*"),
        payload={"ok": True, "user": {"id": _SLACK_USER_ID, "is_restricted": False}},
        repeat=True,
    )
    m.get(
        _re.compile(r"https://slack\.com/api/users\.info.*"),
        payload={"ok": True, "user": {"id": _SLACK_USER_ID, "is_restricted": False}},
        repeat=True,
    )
    m.post(
        "https://slack.com/api/chat.postMessage",
        payload={"ok": True, "ts": "1700000009.000900", "channel": channel_id},
        repeat=True,
    )


async def test_request_agent_key_posts_slack_button_carrying_the_token(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    from aioresponses import aioresponses
    from daimon.core.credential_requests import SLACK_ACTION_ID
    from daimon.core.posted_controls import (
        build_card_blocks,
        build_posted_card,
        card_notification_text,
    )

    tenant = await make_tenant(db_session, platform="slack", workspace_id=_SLACK_TEAM_ID)
    await db_session.commit()
    fernet = await _seed_slack_bot_token(committing_sessionmaker)
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_env_slack", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _slack_runtime(committing_sessionmaker, client=client, fernet=fernet)
    auth = _auth_identity(
        platform="slack",
        external_id=_SLACK_TEAM_ID,
        platform_user_id=_SLACK_USER_ID,
        tenant_id=tenant.id,
    )
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    async with committing_sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=auth.account_id,
            platform=auth.platform or "discord",
            parent_channel_id="C_CRED" if auth.platform == "slack" else "1111",
            thread_id="1700000000.000001" if auth.platform == "slack" else "222",
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )

    with aioresponses() as m:
        _register_slack_post_defaults(m)
        result = await _request_agent_key_impl(
            runtime,
            auth,
            origin_context_id=str(origin.id),
            expected_ma_agent_id="ag_env_slack",
            agent_name="daimon",
            key="OPENAI_API_KEY",
            purpose="calling the OpenAI API",
            channel_id="C_UNTRUSTED",
        )
        import yarl

        posts = m.requests[("POST", yarl.URL("https://slack.com/api/chat.postMessage"))]

    assert result.kind == "env" and result.message_id == "1700000009.000900"
    assert await _row_count(db_session) == 1

    body = posts[0].kwargs["json"]
    actions = [b for b in body["blocks"] if b["type"] == "actions"]
    assert len(actions) == 1, "the posted message must carry exactly one actions block"
    button = actions[0]["elements"][0]
    assert button["action_id"] == SLACK_ACTION_ID
    row = await peek_credential_request(db_session, token=button["value"])
    assert row is not None and row.kind == "env", (
        "the button's value must be the minted single-use token"
    )
    assert row.requester_platform_user_id == _SLACK_USER_ID
    assert "OPENAI_API_KEY" in str(body["blocks"]), "the posted card must name the exact key"
    assert len(button["text"]["text"]) <= 75, "Slack caps button text at 75 characters"

    assert body["channel"] == "C_CRED", "tool arguments cannot redirect a card"
    assert body["thread_ts"] == "1700000000.000001", "private input stays in the origin thread"
    assert row.origin_thread_id == body["thread_ts"], (
        "submission retains the thread after origin expiry"
    )
    assert row.posted_message_id == result.message_id, "outcomes retain the posted card identity"

    expected_card = build_posted_card(
        kind="env",
        state="requested",
        agent_name="daimon",
        responder_name="Daimon",
        target="OPENAI_API_KEY",
        requester_platform_user_id=_SLACK_USER_ID,
        expires_at=row.expires_at,
        token=button["value"],
    )
    assert body["text"] == card_notification_text(expected_card), (
        "the notification text is the card's headline, not a second copy of the copy"
    )
    assert body["blocks"] == build_card_blocks(expected_card, token=button["value"]), (
        "the posted message is exactly the core-rendered requested card"
    )


async def test_request_repo_binding_posts_slack_button(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    from aioresponses import aioresponses

    tenant = await make_tenant(db_session, platform="slack", workspace_id=_SLACK_TEAM_ID)
    await db_session.commit()
    fernet = await _seed_slack_bot_token(committing_sessionmaker)
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_repo_slack", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _slack_runtime(committing_sessionmaker, client=client, fernet=fernet)
    auth = _auth_identity(
        platform="slack",
        external_id=_SLACK_TEAM_ID,
        platform_user_id=_SLACK_USER_ID,
        tenant_id=tenant.id,
    )
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    async with committing_sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=auth.account_id,
            platform=auth.platform or "discord",
            parent_channel_id="C_CRED" if auth.platform == "slack" else "1111",
            thread_id="1700000000.000001" if auth.platform == "slack" else "222",
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )

    with aioresponses() as m:
        _register_slack_post_defaults(m)
        result = await _request_repo_binding_impl(
            runtime,
            auth,
            origin_context_id=str(origin.id),
            expected_ma_agent_id="ag_repo_slack",
            agent_name="daimon",
            repo_url="https://github.com/owner/repo",
            purpose="cloning the project",
            channel_id="C_CRED",
        )

    assert result.kind == "repo"
    assert await _row_count(db_session) == 1


# ---------------------------------------------------------------------------
# Provenance, whole-file import, replacement policy and the waiting task
# ---------------------------------------------------------------------------


async def _seed_origin(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    auth: AuthIdentity,
    responder_name: str = "Daimon",
) -> uuid.UUID:
    """Create a live Discord turn origin and return its id."""
    async with committing_sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant_id,
            account_id=auth.account_id,
            platform="discord",
            parent_channel_id="1111",
            thread_id="222",
            responder_ma_agent_id="ag_daimon",
            responder_name=responder_name,
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )
    return origin.id


async def test_request_agent_key_persists_idempotency_key_and_frozen_target(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_idem", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9301", posted=posted)

    await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_idem",
        agent_name="daimon",
        key="TOGGL_TOKEN",
        purpose="tracking time",
        channel_id="222",
    )

    row = await peek_credential_request(db_session, token=_token_from_posted(posted))
    assert row is not None, "the minted token must resolve to the created row"
    assert isinstance(row.idempotency_key, uuid.UUID), (
        "every minted row carries its own idempotency key"
    )
    assert row.target_ma_agent_id == "ag_idem", (
        "the row freezes the MA agent id the control targets"
    )
    assert row.target_name == "daimon", "the row freezes the target's name at mint time"
    assert row.responder_name == "Daimon", "the row records who picks the work back up"
    assert row.replaces_updated_at is None, "a key that does not exist yet replaces nothing"
    assert row.requested_work is None, "no waiting task was passed"


async def test_request_agent_key_with_none_key_persists_env_file_kind(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_envfile", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9302", posted=posted)

    result = await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_envfile",
        agent_name="daimon",
        key=None,
        purpose="importing a whole .env",
        channel_id="222",
    )

    assert result.kind == "env_file", "an omitted key name requests a whole-file import"
    assert result.target == ENV_FILE_TARGET, (
        "a whole-file request names the fixed .env sentinel, not a key"
    )
    row = await peek_credential_request(db_session, token=_token_from_posted(posted))
    assert row is not None, "the minted token must resolve to the created row"
    assert row.kind == "env_file", "the row records the whole-file kind"
    assert row.target == ENV_FILE_TARGET, "the row records the .env sentinel"
    assert row.replaces_updated_at is None, "a whole-file import replaces no single key"


async def test_request_agent_key_records_replaces_updated_at_for_an_existing_key(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_replace", name="private-bot", tenant_id=tenant.id)]
    )
    # No deployment default and no config rows: "private-bot" answers nowhere,
    # so a member may replace its own key without an admin.
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_replace")
    async with committing_sessionmaker.begin() as session:
        existing = await put_agent_file(
            session,
            tenant_id=tenant.id,
            agent_id=agent_uuid,
            key="TOGGL_TOKEN",
            content="old-value",
            set_by_account_id=auth.account_id,
        )
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9303", posted=posted)

    await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_replace",
        agent_name="private-bot",
        key="TOGGL_TOKEN",
        purpose="rotating the Toggl key",
        channel_id="222",
    )

    row = await peek_credential_request(db_session, token=_token_from_posted(posted))
    assert row is not None, "the minted token must resolve to the created row"
    assert row.replaces_updated_at == existing.updated_at, (
        "a replacement pins the value it saw, so a later write cannot be clobbered"
    )


async def test_request_agent_key_refuses_replacement_on_shared_agent_before_minting(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_shared", name="daimon", tenant_id=tenant.id)]
    )
    # The deployment default makes "daimon" reachable for everyone in the
    # tenant, so replacing a key it already has is an admin operation.
    runtime = _runtime(
        committing_sessionmaker,
        client=client,
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )
    auth = _auth_identity(tenant_id=tenant.id, is_admin=False)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_shared")
    async with committing_sessionmaker.begin() as session:
        await put_agent_file(
            session,
            tenant_id=tenant.id,
            agent_id=agent_uuid,
            key="TOGGL_TOKEN",
            content="old-value",
            set_by_account_id=None,
        )
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9304", posted=posted)

    with pytest.raises(ToolError, match="admin"):
        await _request_agent_key_impl(
            runtime,
            auth,
            origin_context_id=str(origin_id),
            expected_ma_agent_id="ag_shared",
            agent_name="daimon",
            key="TOGGL_TOKEN",
            purpose="rotating the Toggl key",
            channel_id="222",
        )

    assert await _row_count(db_session) == 0, (
        "a refused replacement must mint no credential request row"
    )
    assert posted == {}, "a refused replacement must post no card — nobody is asked for a secret"


async def test_request_agent_key_allows_replacement_for_admin_on_shared_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_shared_admin", name="daimon", tenant_id=tenant.id, managed=True)]
    )
    runtime = _runtime(
        committing_sessionmaker,
        client=client,
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )
    auth = _auth_identity(tenant_id=tenant.id, is_admin=True)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_shared_admin")
    async with committing_sessionmaker.begin() as session:
        existing = await put_agent_file(
            session,
            tenant_id=tenant.id,
            agent_id=agent_uuid,
            key="TOGGL_TOKEN",
            content="old-value",
            set_by_account_id=None,
        )
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9305", posted=posted)

    result = await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_shared_admin",
        agent_name="daimon",
        key="TOGGL_TOKEN",
        purpose="rotating the Toggl key",
        channel_id="222",
    )

    assert result.target == "TOGGL_TOKEN", (
        "an admin may replace a key on the shared, defaults-managed agent"
    )
    row = await peek_credential_request(db_session, token=_token_from_posted(posted))
    assert row is not None, "the minted token must resolve to the created row"
    assert row.replaces_updated_at == existing.updated_at, (
        "the admin's replacement still pins the value the card described"
    )


async def test_pending_task_is_sanitized_and_bounded(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_work", name="research-bot", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)

    echo_posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9306", posted=echo_posted)
    await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_work",
        agent_name="research-bot",
        key="TOGGL_TOKEN",
        purpose="tracking time",
        channel_id="222",
        pending_task="research-bot",
    )
    echo_row = await peek_credential_request(db_session, token=_token_from_posted(echo_posted))
    assert echo_row is not None, "the minted token must resolve to the created row"
    assert echo_row.requested_work is None, (
        "a pending task that only restates the agent's name describes no work"
    )

    long_posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9307", posted=long_posted)
    long_task = "pull the timesheet for " + ("x" * 600)
    await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_work",
        agent_name="research-bot",
        key="OPENAI_API_KEY",
        purpose="calling the OpenAI API",
        channel_id="222",
        pending_task=long_task,
    )
    long_row = await peek_credential_request(db_session, token=_token_from_posted(long_posted))
    assert long_row is not None, "the minted token must resolve to the created row"
    assert long_row.requested_work == long_task[:MAX_REQUESTED_WORK], (
        "a real pending task is kept verbatim, bounded to MAX_REQUESTED_WORK"
    )
    assert long_row.requested_work is not None and len(long_row.requested_work) == (
        MAX_REQUESTED_WORK
    ), "the stored work must be exactly the bound, not longer"


async def test_pending_task_drops_the_key_request_itself_and_keeps_a_real_task(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A save-only ask must not buy a billed continuation; a real task still does."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_work", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)

    ask_posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9310", posted=ask_posted)
    await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_work",
        agent_name="daimon",
        key="HIGGSFIELD_API_KEY",
        purpose="trying Higgsfield",
        channel_id="222",
        pending_task="give Daimon a HIGGSFIELD_API_KEY so people can try it here",
    )
    ask_row = await peek_credential_request(db_session, token=_token_from_posted(ask_posted))
    assert ask_row is not None, "the minted token must resolve to the created row"
    assert ask_row.requested_work is None, (
        "a pending task that only restates the key request describes no work after the save"
    )

    task = "once TOGGL_API_TOKEN is saved, run /root/work/toggl_report.py"
    task_posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9311", posted=task_posted)
    await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_work",
        agent_name="daimon",
        key="TOGGL_API_TOKEN",
        purpose="tracking time",
        channel_id="222",
        pending_task=task,
    )
    task_row = await peek_credential_request(db_session, token=_token_from_posted(task_posted))
    assert task_row is not None, "the minted token must resolve to the created row"
    assert task_row.requested_work == task, (
        "a task naming work beyond the save is kept even when it repeats the key name"
    )


async def test_request_repo_binding_packs_branch_into_target(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_branch", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9308", posted=posted)

    result = await _request_repo_binding_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_branch",
        agent_name="daimon",
        repo_url="https://github.com/owner/repo",
        purpose="cloning the project",
        channel_id="222",
        branch="develop",
    )

    assert result.target == "https://github.com/owner/repo@develop", (
        "the branch rides in the packed target so the click can recover it"
    )
    row = await peek_credential_request(db_session, token=_token_from_posted(posted))
    assert row is not None, "the minted token must resolve to the created row"
    assert row.target == "https://github.com/owner/repo@develop", (
        "the row stores the packed repo@branch target"
    )


async def test_request_repo_binding_rejects_a_branch_carrying_a_delimiter(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()
    with pytest.raises(ToolError, match="branch must not contain"):
        await _request_repo_binding_impl(
            runtime,
            auth,
            agent_name="daimon",
            repo_url="https://github.com/owner/repo",
            purpose="x",
            channel_id="222",
            branch="feat@weird",
        )
    assert await _row_count(db_session) == 0, "a branch that would mangle the target creates no row"


# ---------------------------------------------------------------------------
# The parameter contract: one named key is never a whole-file upload
# ---------------------------------------------------------------------------


async def test_request_agent_key_refuses_a_file_form_when_one_key_is_named_in_purpose(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naming TOGGL_API_TOKEN and omitting `key` asked for the wrong form."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_named", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    _patch_successful_post(monkeypatch, message_id="9501")

    with pytest.raises(ToolError, match="TOGGL_API_TOKEN") as err:
        await _request_agent_key_impl(
            runtime,
            auth,
            origin_context_id=str(origin_id),
            expected_ma_agent_id="ag_named",
            agent_name="daimon",
            key=None,
            purpose="saving the TOGGL_API_TOKEN so the hours report can run",
            channel_id="222",
        )

    assert "`key`" in str(err.value), "the refusal must name the parameter to pass instead"
    assert await _row_count(db_session) == 0, "a refused request mints no row and posts no card"


async def test_request_agent_key_omitted_key_with_no_named_token_mints_env_file(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service name alone ("the Higgsfield key") still gets the file form."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_unnamed", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    _patch_successful_post(monkeypatch, message_id="9502")

    result = await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_unnamed",
        agent_name="daimon",
        key=None,
        purpose="the Higgsfield key and a few others they want to paste in",
        channel_id="222",
    )

    assert result.kind == "env_file", "no UPPER_SNAKE token means no single key was named"
    assert result.target == ENV_FILE_TARGET, "the file form names the .env sentinel"


async def test_result_carries_instruction_and_no_expiry(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card owns the expiry; the result tells the model not to restate it."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_instr", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    _patch_successful_post(monkeypatch, message_id="9503")

    result = await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_instr",
        agent_name="daimon",
        key="TOGGL_API_TOKEN",
        purpose="pulling last week's hours",
        channel_id="222",
    )

    assert "expires_at" not in result.model_dump(), (
        "the model paraphrased the expiry; the result must not hand it one to paraphrase"
    )
    assert set(result.model_dump()) == {"kind", "target", "message_id", "instruction"}, (
        "the result is the four fields the model needs and nothing else"
    )
    assert "expires" in result.instruction, "the instruction must forbid restating the expiry"
    assert "one short sentence" in result.instruction, "the instruction must bound the reply"


# ---------------------------------------------------------------------------
# A second request retires the live one instead of leaving two buttons up
# ---------------------------------------------------------------------------


async def test_second_request_in_same_thread_supersedes_the_live_one_and_edits_its_card(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_super", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    posted: list[dict[str, Any]] = []
    edited: dict[str, dict[str, Any]] = {}
    _patch_post_and_edit(monkeypatch, message_ids=["9601", "9602"], posted=posted, edited=edited)

    first = await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_super",
        agent_name="daimon",
        key=None,
        purpose="taking several keys at once",
        channel_id="222",
    )
    second = await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_super",
        agent_name="daimon",
        key="TOGGL_API_TOKEN",
        purpose="pulling last week's hours",
        channel_id="222",
    )

    assert (first.message_id, second.message_id) == ("9601", "9602"), "both cards were posted"
    old_row = await peek_credential_request(db_session, token=_token_from_posted(posted[0]))
    new_row = await peek_credential_request(db_session, token=_token_from_posted(posted[1]))
    assert old_row is not None and new_row is not None, "both requests must have rows"
    assert old_row.used_at is not None, "the corrected request must no longer be clickable"
    assert old_row.outcome == "replaced_by_newer", "the row says why it was never clicked"
    assert new_row.used_at is None, "the newest request is the one that stays live"

    assert set(edited) == {"9601"}, "only the retired card is edited, and exactly once"
    replaced_card = edited["9601"]["json"]["components"]
    texts = [str(c["content"]) for c in _walk_components(replaced_card) if "content" in c]
    assert texts[0] == f"**{REPLACED_HEADLINE}**", "the retired card announces it was replaced"
    assert "Use the newer form below." in "\n".join(texts), (
        "the retired card points at the live one"
    )
    assert not [
        c
        for c in _walk_components(replaced_card)
        if str(c.get("custom_id", "")).startswith(CUSTOM_ID_PREFIX)
    ], "a replaced card must offer no button"
    assert edited["9601"]["json"]["allowed_mentions"] == {"parse": []}, (
        "re-rendering a card must not ping the requester again"
    )


async def test_requests_for_another_agent_or_requester_are_not_superseded(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supersede scope is one requester, one thread, one agent — nothing wider."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [
            _ma_agent(agent_id="ag_one", name="daimon", tenant_id=tenant.id),
            _ma_agent(agent_id="ag_two", name="research-bot", tenant_id=tenant.id),
        ]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    other_auth = _auth_identity(
        tenant_id=tenant.id, platform_user_id="43", external_id=auth.external_id
    )
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await make_account(db_session, tenant=tenant, id=other_auth.account_id)
    await db_session.commit()
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    other_origin_id = await _seed_origin(
        committing_sessionmaker, tenant_id=tenant.id, auth=other_auth
    )
    posted: list[dict[str, Any]] = []
    edited: dict[str, dict[str, Any]] = {}
    _patch_post_and_edit(
        monkeypatch, message_ids=["9701", "9702", "9703"], posted=posted, edited=edited
    )

    await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_two",
        agent_name="research-bot",
        key="TOGGL_API_TOKEN",
        purpose="pulling hours",
        channel_id="222",
    )
    await _request_agent_key_impl(
        runtime,
        other_auth,
        origin_context_id=str(other_origin_id),
        expected_ma_agent_id="ag_one",
        agent_name="daimon",
        key="TOGGL_API_TOKEN",
        purpose="pulling hours",
        channel_id="222",
    )
    await _request_agent_key_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_one",
        agent_name="daimon",
        key="OPENAI_API_KEY",
        purpose="calling the OpenAI API",
        channel_id="222",
    )

    assert edited == {}, "a different agent and a different requester retire nothing"
    for index, whose in enumerate(("another agent's", "another person's", "its own")):
        row = await peek_credential_request(db_session, token=_token_from_posted(posted[index]))
        assert row is not None and row.used_at is None, f"{whose} request must stay live"


async def test_second_slack_request_in_same_thread_supersedes_the_live_one_and_edits_its_card(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    import yarl
    from aioresponses import aioresponses

    tenant = await make_tenant(db_session, platform="slack", workspace_id=_SLACK_TEAM_ID)
    await db_session.commit()
    fernet = await _seed_slack_bot_token(committing_sessionmaker)
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_slack_super", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _slack_runtime(committing_sessionmaker, client=client, fernet=fernet)
    auth = _auth_identity(
        platform="slack",
        external_id=_SLACK_TEAM_ID,
        platform_user_id=_SLACK_USER_ID,
        tenant_id=tenant.id,
    )
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    async with committing_sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=auth.account_id,
            platform="slack",
            parent_channel_id="C_CRED",
            thread_id="1700000000.000001",
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )

    with aioresponses() as m:
        _register_slack_post_defaults(m)
        m.post(
            "https://slack.com/api/chat.update",
            payload={"ok": True, "ts": "1700000009.000900", "channel": "C_CRED"},
            repeat=True,
        )
        await _request_agent_key_impl(
            runtime,
            auth,
            origin_context_id=str(origin.id),
            expected_ma_agent_id="ag_slack_super",
            agent_name="daimon",
            key=None,
            purpose="taking several keys at once",
            channel_id="C_CRED",
        )
        await _request_agent_key_impl(
            runtime,
            auth,
            origin_context_id=str(origin.id),
            expected_ma_agent_id="ag_slack_super",
            agent_name="daimon",
            key="TOGGL_API_TOKEN",
            purpose="pulling last week's hours",
            channel_id="C_CRED",
        )
        posts = m.requests[("POST", yarl.URL("https://slack.com/api/chat.postMessage"))]
        updates = m.requests[("POST", yarl.URL("https://slack.com/api/chat.update"))]

    assert len(posts) == 2, "each request posts its own card"
    assert len(updates) == 1, "exactly the one retired card is updated"
    update_body = updates[0].kwargs["json"]
    assert update_body["ts"] == "1700000009.000900", "the update targets the first card"
    assert update_body["channel"] == "C_CRED", "the update goes to the channel the card is in"
    assert REPLACED_HEADLINE in str(update_body["blocks"]), (
        "the retired card announces it was replaced"
    )
    assert not [b for b in update_body["blocks"] if b["type"] == "actions"], (
        "a replaced card must offer no button"
    )

    old_token = posts[0].kwargs["json"]["blocks"][2]["elements"][0]["value"]
    old_row = await peek_credential_request(db_session, token=old_token)
    assert old_row is not None and old_row.outcome == "replaced_by_newer", (
        "the retired Slack request records why it was never clicked"
    )


# ---------------------------------------------------------------------------
# Teams: an Adaptive Card in the origin conversation, edited when replaced
# ---------------------------------------------------------------------------

_TEAMS_CALLER = "11111111-2222-3333-4444-555555555555"
_TEAMS_CHAT = "a:teams-chat"


def _teams_client(requests: list[httpx.Request]) -> TeamsBotClient:
    def route(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "login.microsoftonline.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if "/members/" in str(request.url):
            return httpx.Response(200, json={"aadObjectId": _TEAMS_CALLER})
        return httpx.Response(200, json={"id": f"act-{len(requests)}"})

    return TeamsBotClient(
        httpx.AsyncClient(transport=httpx.MockTransport(route)),
        client_id="app-id",
        client_secret="secret",
        tenant_id="entra",
    )


async def _teams_setup(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> tuple[McpRuntime, AuthIdentity, str, list[httpx.Request]]:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_t", name="daimon", tenant_id=tenant.id)]
    )
    requests: list[httpx.Request] = []
    runtime = dataclasses.replace(
        _runtime(sessionmaker, client=client), teams_client=_teams_client(requests)
    )
    auth = _auth_identity(
        platform="teams", external_id="entra", platform_user_id=_TEAMS_CALLER, tenant_id=tenant.id
    )
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    async with sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=auth.account_id,
            platform="teams",
            parent_channel_id=_TEAMS_CHAT,
            thread_id=_TEAMS_CHAT,
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )
    return runtime, auth, str(origin.id), requests


async def test_request_agent_key_on_teams_posts_a_card_and_edits_it_when_replaced(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    runtime, auth, origin_id, requests = await _teams_setup(committing_sessionmaker, db_session)
    ask = {
        "origin_context_id": origin_id,
        "expected_ma_agent_id": "ag_t",
        "agent_name": "daimon",
        "key": "OPENAI_API_KEY",
        "purpose": "x",
        "channel_id": "untrusted",
    }

    first = await _request_agent_key_impl(runtime, auth, **ask)
    await _request_agent_key_impl(runtime, auth, **ask)

    writes = [r for r in requests if r.method in ("POST", "PUT") and "login." not in str(r.url)]
    post, _second, put = writes
    assert str(post.url).endswith(f"/v3/conversations/{_TEAMS_CHAT}/activities"), "origin chat"
    card = json.loads(post.content)["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    row = await peek_credential_request(
        db_session, token=card["content"]["body"][3]["actions"][0]["data"]["token"]
    )
    assert row is not None and row.platform == "teams" and row.posted_message_id == first.message_id
    assert str(put.url).endswith(f"/activities/{first.message_id}"), "the old card is edited"
    assert REPLACED_HEADLINE in put.content.decode(), "and says it was replaced"


async def test_request_agent_key_on_teams_refuses_an_env_file(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    runtime, auth, origin_id, requests = await _teams_setup(committing_sessionmaker, db_session)
    with pytest.raises(ToolError, match="Request each key by name"):
        await _request_agent_key_impl(
            runtime,
            auth,
            origin_context_id=origin_id,
            expected_ma_agent_id="ag_t",
            agent_name="daimon",
            key=None,
            purpose="x",
            channel_id="c",
        )
    assert await _row_count(db_session) == 0 and not requests, "nothing minted or posted"


@pytest.mark.parametrize("key", ["OPENAI_API_KEY", None], ids=["one-key", "env-file"])
async def test_request_agent_key_refuses_before_posting_when_no_crypto_keys(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    key: str | None,
) -> None:
    """H3: without DAIMON_CRYPTO__KEYS nobody is asked for a value that would be stored plain."""
    keyless = async_sessionmaker(
        bind=committing_sessionmaker.kw["bind"],
        expire_on_commit=False,
        info={"crypto_keys": (), "crypto_allow_plaintext": False},
    )
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_env", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(keyless, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9501", posted=posted)

    with pytest.raises(ToolError, match="DAIMON_CRYPTO__KEYS"):
        await _request_agent_key_impl(
            runtime,
            auth,
            origin_context_id=str(origin_id),
            expected_ma_agent_id="ag_env",
            agent_name="daimon",
            key=key,
            purpose="the CRM key",
            channel_id="222",
        )
    assert await _row_count(db_session) == 0
    assert posted == {}


# ---------------------------------------------------------------------------
# mcp_replace: repointing an existing server needs an admin on a shared agent
# ---------------------------------------------------------------------------


def _ma_agent_with_server(
    *, agent_id: str, tenant_id: uuid.UUID, server_name: str, url: str
) -> dict[str, object]:
    agent = _ma_agent(agent_id=agent_id, name="daimon", tenant_id=tenant_id)
    agent["mcp_servers"] = [{"name": server_name, "type": "url", "url": url}]
    return agent


@pytest.mark.parametrize("kind", ["token", "oauth"])
async def test_request_mcp_refuses_repointing_an_existing_server_on_a_shared_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """H2: a member must not repoint a live agent's `linear` at their own URL."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [
            _ma_agent_with_server(
                agent_id="ag_shared",
                tenant_id=tenant.id,
                server_name="linear",
                url="https://mcp.linear.app/sse",
            )
        ]
    )
    runtime = _runtime(
        committing_sessionmaker,
        client=client,
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )
    auth = _auth_identity(tenant_id=tenant.id, is_admin=False)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9401", posted=posted)
    impl = _request_mcp_token_impl if kind == "token" else _request_mcp_oauth_impl

    with pytest.raises(ToolError, match="admin"):
        await impl(
            runtime,
            auth,
            origin_context_id=str(origin_id),
            expected_ma_agent_id="ag_shared",
            agent_name="daimon",
            server_name="linear",
            url="https://attacker.example/mcp",
            channel_id="222",
        )

    assert await _row_count(db_session) == 0, "a refused replacement must mint no request row"
    assert posted == {}, "a refused replacement must post no card"


async def test_request_mcp_token_refuses_overwriting_the_shared_token_for_a_url(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent-wide token for a URL is mirrored to every caller: overwriting it is a replace."""
    from cryptography.fernet import Fernet, MultiFernet
    from daimon.core.agent_mcp_credentials import save_agent_mcp_credential

    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [
            _ma_agent_with_server(
                agent_id="ag_shared",
                tenant_id=tenant.id,
                server_name="linear",
                url="https://mcp.linear.app/sse",
            )
        ]
    )
    runtime = _runtime(
        committing_sessionmaker,
        client=client,
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )
    await save_agent_mcp_credential(
        sessionmaker=committing_sessionmaker,
        fernet=MultiFernet([Fernet(Fernet.generate_key())]),
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_shared"),
        mcp_server_url="https://mcp.linear.app/sse",
        plaintext_token="existing-token",
    )
    auth = _auth_identity(tenant_id=tenant.id, is_admin=False)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9402", posted=posted)

    with pytest.raises(ToolError, match="admin"):
        await _request_mcp_token_impl(
            runtime,
            auth,
            origin_context_id=str(origin_id),
            expected_ma_agent_id="ag_shared",
            agent_name="daimon",
            server_name="linear",
            url="https://mcp.linear.app/sse",
            channel_id="222",
        )
    assert posted == {}


@pytest.mark.parametrize(
    ("is_admin", "reachable", "server_name"),
    [(True, True, "linear"), (False, False, "linear"), (False, True, "notion")],
    ids=["admin-on-shared", "member-on-private-draft", "member-new-name"],
)
async def test_request_mcp_token_still_allows_admins_drafts_and_new_names(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    is_admin: bool,
    reachable: bool,
    server_name: str,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [
            _ma_agent_with_server(
                agent_id="ag_shared",
                tenant_id=tenant.id,
                server_name="linear",
                url="https://mcp.linear.app/sse",
            )
        ]
    )
    runtime = _runtime(
        committing_sessionmaker,
        client=client,
        deployment_default=DeploymentDefault(agent_name="daimon" if reachable else "other"),
    )
    auth = _auth_identity(tenant_id=tenant.id, is_admin=is_admin)
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    origin_id = await _seed_origin(committing_sessionmaker, tenant_id=tenant.id, auth=auth)
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9403", posted=posted)

    result = await _request_mcp_token_impl(
        runtime,
        auth,
        origin_context_id=str(origin_id),
        expected_ma_agent_id="ag_shared",
        agent_name="daimon",
        server_name=server_name,
        url="https://mcp.example.com/mcp",
        channel_id="222",
    )
    assert result.message_id == "9403"
