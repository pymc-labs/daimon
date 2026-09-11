"""RBAC integration tests — admin vs non-admin tool visibility.

All tests use httpx.ASGITransport because enable_components requires a real
HTTP session context. In-memory fastmcp.Client would give false results.

Protocol sequence (MCP Streamable HTTP):
1. POST /mcp with initialize body → establishes session, returns Mcp-Session-Id header
2. POST /mcp with method body + Mcp-Session-Id header → actual call
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from daimon.core.mcp_auth import mint_jwt
from daimon.core.stores import accounts
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp

from .factories import make_jwt, mcp_session

pytestmark = pytest.mark.asyncio

SECRET = "a" * 32
_NOW = dt.datetime(2026, 4, 24, tzinfo=dt.UTC)


def _make_app(sessionmaker: async_sessionmaker[AsyncSession]) -> ASGIApp:
    """Create a test MCP app with JWT auth wired to the test DB."""
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr(SECRET), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
    )


async def _seed_admin_and_user(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[str, str]:
    """Seed one admin and one user account in the same tenant.

    Returns (admin_token, user_token).
    """
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="guild-rbac-test")
        admin_account = await make_account(s, tenant=tenant)
        await accounts.set_role(s, admin_account.id, Role.ADMIN)
        user_account = await make_account(s, tenant=tenant)
        admin_token = make_jwt(account_id=admin_account.id)
        user_token = make_jwt(account_id=user_account.id)
    return admin_token, user_token


async def test_non_admin_list_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Non-admin tools/list returns only search_tools and call_tool.

    tools/list is NOT the discovery surface for individual tools — the BM25
    search transform collapses the full catalog into these two meta-tools for
    every session, admin included. list_credentials is agent-chat-tagged, so
    it never appears here for a session with no agent identity — same as any
    other mutating tool name, that per-tool check lives against search_tools
    instead (see test_non_admin_search_includes_relaxed_tool and the other
    search_tools-based tests below)."""
    admin_token, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await mcp_session(app, token=user_token, method="tools/list")
    tools_payload = result.get("result", result)
    tool_names: list[str] = [t["name"] for t in tools_payload.get("tools", [])]  # type: ignore[union-attr]

    # Only meta-tools visible to non-admins
    for expected in ("search_tools", "call_tool"):
        assert expected in tool_names, (
            f"Expected {expected!r} in non-admin tool list, got: {tool_names}"
        )
    assert "list_credentials" not in tool_names, (
        f"list_credentials is agent-chat-tagged and must not appear for a "
        f"session with no agent identity; got: {tool_names}"
    )


async def test_non_admin_search_excludes_admin_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Non-admin search_tools('archive agent') must not surface archive_agent."""
    admin_token, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await mcp_session(
        app,
        token=user_token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": "archive agent"}},
    )
    call_result = result.get("result", result)
    # Result is a list of content items; text output should have no admin tool names
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    assert "archive_agent" not in output_text, (
        f"Admin tool name in non-admin search result: {output_text!r}"
    )


async def test_non_admin_call_admin_tool_blocked(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Non-admin calling call_tool('archive_agent') gets a not-found error.

    After the admin-tag sweep every gated mutating tool carries tags={'admin'},
    so fastmcp's get_tool filters them for non-admin sessions before any impl
    runs. The impl gate remains as defense-in-depth."""
    admin_token, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await mcp_session(
        app,
        token=user_token,
        method="tools/call",
        params={
            "name": "call_tool",
            "arguments": {
                "name": "archive_agent",
                "arguments": {"name": "daimon"},
            },
        },
    )
    # The call should succeed at HTTP level but return a tool error (isError=True)
    # or the outer call_tool reports the tool as not found
    call_result = result.get("result", result)
    is_error = call_result.get("isError", False)  # type: ignore[union-attr]
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    # Either isError flag or error message in content indicates rejection
    assert is_error or "not found" in output_text.lower() or "error" in output_text.lower(), (
        f"Expected blocked call for non-admin; isError={is_error!r}, output={output_text!r}"
    )


RELAXED_TOOL_NAMES = (
    "create_agent",
    "update_agent",
    "attach_mcp_server",
    "fork_agent",
    "create_environment",
)
"""Tools whose blast radius is one agent, or nothing at all, rather than the
whole tenant — relaxed onto the open (non-admin-visible, non-admin-callable)
surface. Untagged, so a non-admin chat session's search surfaces them.

`create_environment` is the "nothing at all" case: the environment it creates is
unreachable until an admin scopes an agent onto it via the gated
`set_agent_default`."""

AGENT_IDENTITY_SCOPED_TOOL_NAMES = (
    "set_repo_binding",
    "clear_repo_binding",
    "self_write_file",
    "self_delete_file",
)
"""Four of the self_edit.py tools, tagged agent-chat: visible only to a
session whose token carries an agent identity, never to an ordinary
non-admin chat session's search."""

CHAT_REMOVAL_TOOL_NAMES = (
    "detach_mcp_server",
    "remove_skill",
    "remove_agent_key",
    "list_agent_keys",
)
"""The four chat-reachable removal tools: never admin-tagged, so they are
discoverable by a non-admin session's search_tools like RELAXED_TOOL_NAMES,
but also must never leak into a narrowed agent-chat session's tools/list."""


@pytest.mark.parametrize("tool_name", CHAT_REMOVAL_TOOL_NAMES)
async def test_non_admin_search_includes_chat_removal_tool(
    sessionmaker: async_sessionmaker[AsyncSession],
    tool_name: str,
) -> None:
    """Each chat-removal tool is discoverable by a non-admin session via search_tools."""
    _, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await mcp_session(
        app,
        token=user_token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": tool_name.replace("_", " ")}},
    )
    call_result = result.get("result", result)
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    assert tool_name in output_text, (
        f"{tool_name} must be discoverable by a non-admin session; got: {output_text!r}"
    )


async def test_agent_id_claim_session_discovers_none_of_the_chat_removal_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A session narrowed to agent-chat tools (an agent_id claim) must not
    discover any of the four chat-removal tools — being untagged (not
    admin-only) must not accidentally grant agent-chat visibility."""
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="agent-chat-removal-rbac")
        account = await make_account(s, tenant=tenant)
    token = mint_jwt(account_id=account.id, secret=SECRET.encode(), now=_NOW, agent_id=uuid.uuid4())
    app = _make_app(sessionmaker)

    result = await mcp_session(app, token=token, method="tools/list")
    payload = result.get("result", result)
    tool_names = {t["name"] for t in payload.get("tools", [])}  # type: ignore[union-attr]

    assert not (set(CHAT_REMOVAL_TOOL_NAMES) & tool_names), (
        f"an agent_id-claim session must not discover any chat-removal tool; got: {tool_names}"
    )


@pytest.mark.parametrize("tool_name", RELAXED_TOOL_NAMES)
async def test_non_admin_search_includes_relaxed_tool(
    sessionmaker: async_sessionmaker[AsyncSession],
    tool_name: str,
) -> None:
    """Each relaxed tool is discoverable by a non-admin session via search_tools."""
    _, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await mcp_session(
        app,
        token=user_token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": tool_name.replace("_", " ")}},
    )
    call_result = result.get("result", result)
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    assert tool_name in output_text, (
        f"{tool_name} must be discoverable by a non-admin session; got: {output_text!r}"
    )


@pytest.mark.parametrize("tool_name", AGENT_IDENTITY_SCOPED_TOOL_NAMES)
async def test_non_admin_search_excludes_agent_identity_scoped_tool(
    sessionmaker: async_sessionmaker[AsyncSession],
    tool_name: str,
) -> None:
    """Each agent-identity-scoped self_edit tool stays undiscoverable to a
    non-admin chat session's search_tools — the agent-chat tag hides it from
    any session whose token carries no agent identity."""
    _, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await mcp_session(
        app,
        token=user_token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": tool_name.replace("_", " ")}},
    )
    call_result = result.get("result", result)
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    assert tool_name not in output_text, (
        f"{tool_name} must NOT be discoverable by a non-admin chat session; got: {output_text!r}"
    )


async def test_non_admin_call_self_write_file_refused(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A non-admin chat session's call_tool invocation of self_write_file is
    refused — the agent-chat tag hides it from tools/list and get_tool, so
    the call never reaches the implementation. This is the inverted record
    of the tool leaving the chat surface: it used to reach the impl and fail
    inside with "agent_id missing"; now it is refused before that."""
    _, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await mcp_session(
        app,
        token=user_token,
        method="tools/call",
        params={
            "name": "call_tool",
            "arguments": {
                "name": "self_write_file",
                "arguments": {"key": "notes", "content": "hello"},
            },
        },
    )
    call_result = result.get("result", result)
    is_error = call_result.get("isError", False)  # type: ignore[union-attr]
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(
        item.get("text", "") for item in content if isinstance(item, dict)
    ).lower()
    assert is_error or "not found" in output_text, (
        f"self_write_file must be refused for a non-admin chat caller; "
        f"isError={is_error!r}, output={output_text!r}"
    )


STILL_ADMIN_TOOL_NAMES = (
    "set_agent_default",
    "clear_agent_default",
    "archive_agent",
    "sync_skills",
    "update_environment",
    "archive_environment",
)
"""Tools whose blast radius is the whole tenant, and stay admin-only.

`create_environment` is deliberately absent: a new environment is inert until an
admin scopes an agent onto it, so its blast radius is nothing until a gated call
widens it. Mutating an environment others already resolve to is a different
matter, which is why `update_environment` and `archive_environment` stay here.
"""


@pytest.mark.parametrize("tool_name", STILL_ADMIN_TOOL_NAMES)
async def test_non_admin_search_excludes_remaining_admin_tool(
    sessionmaker: async_sessionmaker[AsyncSession],
    tool_name: str,
) -> None:
    """Each tenant-wide tool stays invisible to a non-admin session's search_tools.

    Asserted against the ``### <name>`` heading the renderer gives every returned
    tool, not against the bare name anywhere in the output. Docstrings cross-
    reference each other by tool name — ``explain_agent_resolution`` warns about
    ``set_agent_default``, ``remove_skill`` contrasts itself with ``delete_skill``
    — so a substring search cannot tell "this tool is exposed" from "some visible
    tool mentions it in prose". The heading only appears for a tool the filter
    actually returned, which is the property being protected.
    """
    _, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await mcp_session(
        app,
        token=user_token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": tool_name.replace("_", " ")}},
    )
    call_result = result.get("result", result)
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    assert f"### {tool_name}" not in output_text, (
        f"{tool_name} must NOT be discoverable by a non-admin session; got: {output_text!r}"
    )


async def test_discord_vault_token_is_admin_claim_without_internal_denied_admin_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A Discord vault token with is_admin=True but no internal claim
    must NOT gain admin tool visibility.

    The old behavior (pre-88-03) elevated guild admins via is_admin alone. That was the
    RBAC escalation bug: a stale pre-sweep Discord vault credential baked with is_admin=True
    could ride a non-admin caller's session into admin tooling. Closed by requiring the
    internal discriminator claim (emitted only by mint_internal_mcp_token)."""
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="guild-isadmin-test")
        user_account = await make_account(s, tenant=tenant)
        # Discord vault token: is_admin=True but no internal claim
        guild_admin_token = make_jwt(account_id=user_account.id, is_admin=True)
    app = _make_app(sessionmaker)

    result = await mcp_session(
        app,
        token=guild_admin_token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": "sync skills"}},
    )
    call_result = result.get("result", result)
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    assert "### sync_skills" not in output_text, (
        f"Discord vault token with is_admin but no internal must NOT see sync_skills; got: {output_text!r}"
    )


async def test_admin_search_includes_admin_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Admin search_tools('create agent') returns create_agent in results."""
    admin_token, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await mcp_session(
        app,
        token=admin_token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": "create agent"}},
    )
    call_result = result.get("result", result)
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    assert "create_agent" in output_text, (
        f"create_agent not in admin search result: {output_text!r}"
    )


async def test_admin_search_includes_fork_agent(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Admin search_tools('fork') returns fork_agent — fork_agent is untagged
    (open to every session) after this plan's relaxation, so it must remain
    discoverable for admin sessions too."""
    admin_token, _ = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await mcp_session(
        app,
        token=admin_token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": "fork agent"}},
    )
    call_result = result.get("result", result)
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    assert "fork_agent" in output_text, (
        f"fork_agent must be discoverable by admin after tag sweep; got: {output_text!r}"
    )


async def test_admin_list_tools_returns_meta_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Admin tools/list returns the BM25 meta-tools (search_tools, call_tool).

    The BM25SearchTransform collapses the full tool catalog into meta-tools for
    all users. Admin tools are accessible via search_tools/call_tool (already
    tested in test_admin_search_includes_admin_tools). The tools/list response
    is identical for admin and non-admin in terms of visible tool names — the
    difference is that admin call_tool calls against admin tools are allowed.
    list_credentials is agent-chat-tagged, not admin-tagged, so an
    admin-but-not-agent-identity session does not see it here either.
    """
    admin_token, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await mcp_session(app, token=admin_token, method="tools/list")
    tools_payload = result.get("result", result)
    tool_names: list[str] = [t["name"] for t in tools_payload.get("tools", [])]  # type: ignore[union-attr]

    for expected in ("search_tools", "call_tool"):
        assert expected in tool_names, (
            f"Expected meta-tool {expected!r} missing from admin tool list: {tool_names}"
        )
    assert "list_credentials" not in tool_names, (
        f"list_credentials is agent-chat-tagged and must not appear for an "
        f"admin session with no agent identity; got: {tool_names}"
    )
