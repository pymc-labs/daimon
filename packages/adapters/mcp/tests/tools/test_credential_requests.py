"""DB-backed unit tests for the credential-request MCP tools.

Each test calls the private ``_*_impl`` functions directly with a real
sessionmaker (real Postgres), a transport-level Anthropic fake (``MARouter``)
for the agent-name resolution, and a transport-level patched Discord
``HTTPClient`` (``patch_discord_http``, loaded from the sibling conftest.py by
file path — see test_discord.py for the same pattern) for the button post.
"""

from __future__ import annotations

import importlib.util
import inspect
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import daimon.adapters.mcp.tools.credential_requests as _credential_requests_mod
import discord.http
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.beta_managed_agents_agent import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_model_config import (
    BetaManagedAgentsModelConfig,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
)
from daimon.core.credential_requests import CUSTOM_ID_PREFIX, DEFAULT_TTL
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.credential_requests import peek_credential_request
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_tenant
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

_request_env_credential_impl = (
    _credential_requests_mod._request_env_credential_impl  # pyright: ignore[reportPrivateUsage]
)
_request_mcp_credential_impl = (
    _credential_requests_mod._request_mcp_credential_impl  # pyright: ignore[reportPrivateUsage]
)
_request_repo_binding_impl = (
    _credential_requests_mod._request_repo_binding_impl  # pyright: ignore[reportPrivateUsage]
)
register_credential_request_tools = _credential_requests_mod.register_credential_request_tools

pytestmark = pytest.mark.asyncio

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
        deployment_default=DeploymentDefault(),
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


def _ma_agent(*, agent_id: str, name: str, tenant_id: uuid.UUID) -> dict[str, object]:
    agent = BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name=name,
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        system=None,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: name,
        },
        mcp_servers=[],
        tools=[],
        skills=[],
        created_at="2026-05-19T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-05-19T00:00:00Z",  # type: ignore[arg-type]
        archived_at=None,
        description=None,
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
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)


def _token_from_posted(posted: dict[str, Any]) -> str:
    custom_id: str = posted["json"]["components"][0]["components"][0]["custom_id"]
    assert custom_id.startswith(CUSTOM_ID_PREFIX), f"unexpected custom_id shape: {custom_id!r}"
    return custom_id[len(CUSTOM_ID_PREFIX) :]


async def _row_count(db_session: AsyncSession) -> int:
    result = await db_session.execute(text("SELECT COUNT(*) FROM credential_requests"))
    return result.scalar_one()


# ---------------------------------------------------------------------------
# 1. request_env_credential creates a row + posts a button
# ---------------------------------------------------------------------------


async def test_request_env_credential_creates_row_and_posts_button(
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
    before = datetime.now(UTC)
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9201", posted=posted)

    result = await _request_env_credential_impl(
        runtime,
        auth,
        agent_name="daimon",
        key="OPENAI_API_KEY",
        purpose="calling the OpenAI API",
        channel_id="222",
    )

    assert result.kind == "env", "result must report the env kind"
    assert result.target == "OPENAI_API_KEY", "result must report the exact key"
    assert result.message_id == "9201", "result must report the posted message id"
    assert before + DEFAULT_TTL <= result.expires_at, "expires_at must be at least now + TTL"
    assert await _row_count(db_session) == 1, "exactly one credential_requests row must be created"

    row = await peek_credential_request(db_session, token=_token_from_posted(posted))
    assert row is not None, "the minted token must resolve to the created row"
    assert row.kind == "env"
    assert row.mcp_server_url is None, "env rows must not carry an mcp_server_url"
    assert row.account_id == auth.account_id, "row must stamp the caller's account_id"
    assert row.requester_platform_user_id == auth.platform_user_id, (
        "row must stamp the caller's platform_user_id"
    )
    assert row.tenant_id == tenant.id


# ---------------------------------------------------------------------------
# 2. request_mcp_credential creates a row + posts a button
# ---------------------------------------------------------------------------


async def test_request_mcp_credential_creates_row_and_posts_button(
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
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9202", posted=posted)

    result = await _request_mcp_credential_impl(
        runtime,
        auth,
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


# ---------------------------------------------------------------------------
# 3. Neither registered tool exposes a token/secret/value parameter
# ---------------------------------------------------------------------------


async def test_neither_tool_signature_exposes_a_secret_bearing_parameter() -> None:
    mcp = FastMCP(name="test")
    register_credential_request_tools(mcp, _runtime(MagicMock()))  # type: ignore[arg-type]
    tools = await mcp.list_tools()
    by_name = {t.name: t for t in tools}
    assert {"request_env_credential", "request_mcp_credential"} <= by_name.keys(), (
        "both tools must be registered"
    )
    for name in ("request_env_credential", "request_mcp_credential"):
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
    for fn in (_request_env_credential_impl, _request_mcp_credential_impl):
        params = set(inspect.signature(fn).parameters)
        forbidden = {p for p in params if any(w in p.lower() for w in ("token", "secret", "value"))}
        assert not forbidden, (
            f"{fn.__name__} must not accept a secret-bearing parameter, found {forbidden}"
        )


# ---------------------------------------------------------------------------
# 4. A non-admin caller succeeds — there is no admin gate
# ---------------------------------------------------------------------------


async def test_request_env_credential_succeeds_for_non_admin_caller(
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
    _patch_successful_post(monkeypatch, message_id="9203")

    result = await _request_env_credential_impl(
        runtime,
        auth,
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


async def test_request_env_credential_rejects_slack_caller_without_workspace(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(platform="slack", external_id=None, platform_user_id="U123")
    with pytest.raises(ToolError, match="workspace context"):
        await _request_env_credential_impl(
            runtime, auth, agent_name="daimon", key="OPENAI_API_KEY", purpose="x", channel_id="222"
        )
    assert await _row_count(db_session) == 0, "a rejected slack call must create no row"


async def test_request_mcp_credential_rejects_slack_caller_without_workspace(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(platform="slack", external_id=None, platform_user_id="U123")
    with pytest.raises(ToolError, match="workspace context"):
        await _request_mcp_credential_impl(
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


async def test_request_env_credential_rejects_missing_platform_user_id(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(platform_user_id=None)
    with pytest.raises(ToolError, match="platform-bound identity"):
        await _request_env_credential_impl(
            runtime, auth, agent_name="daimon", key="OPENAI_API_KEY", purpose="x", channel_id="222"
        )
    assert await _row_count(db_session) == 0


# ---------------------------------------------------------------------------
# 7. An env key failing the POSIX rule gets a ToolError; no row created
# ---------------------------------------------------------------------------


async def test_request_env_credential_rejects_invalid_key(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()
    with pytest.raises(ToolError, match=r"\[A-Za-z_\]\[A-Za-z0-9_\]\*"):
        await _request_env_credential_impl(
            runtime, auth, agent_name="daimon", key="1BAD-KEY", purpose="x", channel_id="222"
        )
    assert await _row_count(db_session) == 0


# ---------------------------------------------------------------------------
# 8. An agent_name with no matching MA agent gets a ToolError; no row created
# ---------------------------------------------------------------------------


async def test_request_env_credential_rejects_unknown_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents([])  # no agents in the tenant
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    with pytest.raises(ToolError, match="not found in this tenant"):
        await _request_env_credential_impl(
            runtime, auth, agent_name="ghost", key="OPENAI_API_KEY", purpose="x", channel_id="222"
        )
    assert await _row_count(db_session) == 0


# ---------------------------------------------------------------------------
# 9. An mcp url that is not http(s) gets a ToolError; no row created
# ---------------------------------------------------------------------------


async def test_request_mcp_credential_rejects_non_http_url(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()
    with pytest.raises(ToolError, match="must be http or https"):
        await _request_mcp_credential_impl(
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
    before = datetime.now(UTC)
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9204", posted=posted)

    result = await _request_repo_binding_impl(
        runtime,
        auth,
        agent_name="daimon",
        repo_url="https://github.com/clsandoval/daimon-qa-scratch",
        purpose="binding the QA scratch repo",
        channel_id="222",
    )

    assert result.kind == "repo", "result must report the repo kind"
    assert result.target == "https://github.com/clsandoval/daimon-qa-scratch", (
        "result must report the exact submitted repo url"
    )
    assert result.message_id == "9204", "result must report the posted message id"
    assert before + DEFAULT_TTL <= result.expires_at, "expires_at must be at least now + TTL"
    assert await _row_count(db_session) == 1, "exactly one credential_requests row must be created"

    row = await peek_credential_request(db_session, token=_token_from_posted(posted))
    assert row is not None, "the minted token must resolve to the created row"
    assert row.kind == "repo"
    assert row.target == "https://github.com/clsandoval/daimon-qa-scratch"
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
    with pytest.raises(ToolError, match="not found in this tenant"):
        await _request_repo_binding_impl(
            runtime,
            auth,
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
    posted: dict[str, Any] = {}
    _patch_successful_post(monkeypatch, message_id="9205", posted=posted)

    await _request_repo_binding_impl(
        runtime,
        auth,
        agent_name="daimon",
        repo_url="https://github.com/clsandoval/daimon-qa-scratch",
        purpose="binding the QA scratch repo",
        channel_id="222",
    )

    content = posted["json"]["content"]
    assert "daimon" in content, "message body must name the agent"
    assert "https://github.com/clsandoval/daimon-qa-scratch" in content, (
        "message body must name the exact repo"
    )
    button = posted["json"]["components"][0]["components"][0]
    assert button["custom_id"].startswith(CUSTOM_ID_PREFIX), (
        "the button's custom_id must carry the credential-request prefix"
    )
    assert button["label"].startswith("Bind repo: "), (
        "the button's label must use the repo-kind prefix"
    )


async def test_request_env_credential_raises_when_button_post_fails(
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

    with pytest.raises(ToolError, match="posting the button failed"):
        await _request_env_credential_impl(
            runtime, auth, agent_name="daimon", key="OPENAI_API_KEY", purpose="x", channel_id="222"
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


async def test_request_env_credential_posts_slack_button_carrying_the_token(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    from aioresponses import aioresponses
    from daimon.core.credential_requests import SLACK_ACTION_ID

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

    with aioresponses() as m:
        _register_slack_post_defaults(m)
        result = await _request_env_credential_impl(
            runtime,
            auth,
            agent_name="daimon",
            key="OPENAI_API_KEY",
            purpose="calling the OpenAI API",
            channel_id="C_CRED",
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
    assert "OPENAI_API_KEY" in button["text"]["text"]
    assert len(button["text"]["text"]) <= 75, "Slack caps button text at 75 characters"


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

    with aioresponses() as m:
        _register_slack_post_defaults(m)
        result = await _request_repo_binding_impl(
            runtime,
            auth,
            agent_name="daimon",
            repo_url="https://github.com/owner/repo",
            purpose="cloning the project",
            channel_id="C_CRED",
        )

    assert result.kind == "repo"
    assert await _row_count(db_session) == 1
