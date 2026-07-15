"""RBAC integration tests — admin vs non-admin tool visibility.

All tests use httpx.ASGITransport because enable_components requires a real
HTTP session context. In-memory fastmcp.Client would give false results.

Protocol sequence (MCP Streamable HTTP):
1. POST /mcp with initialize body → establishes session, returns Mcp-Session-Id header
2. POST /mcp with method body + Mcp-Session-Id header → actual call
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core._models import Account
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from daimon.testing.factories import make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Message

from .factories import make_jwt

pytestmark = pytest.mark.asyncio

SECRET = "a" * 32

INIT_BODY: dict[str, object] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
INIT_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@contextlib.asynccontextmanager
async def _lifespan(app: ASGIApp) -> AsyncIterator[None]:
    send_queue: asyncio.Queue[Message] = asyncio.Queue()
    receive_queue: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return await receive_queue.get()

    async def send(message: Message) -> None:
        await send_queue.put(message)

    async def run_lifespan() -> None:
        await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    task = asyncio.create_task(run_lifespan())

    await receive_queue.put({"type": "lifespan.startup"})
    msg = await send_queue.get()
    assert msg["type"] == "lifespan.startup.complete", msg
    try:
        yield
    finally:
        await receive_queue.put({"type": "lifespan.shutdown"})
        msg = await send_queue.get()
        assert msg["type"] == "lifespan.shutdown.complete", msg
        await task


async def _mcp_session(
    app: ASGIApp,
    *,
    token: str,
    method: str,
    params: dict[str, object] | None = None,
) -> dict[str, object]:
    """Initialize MCP session then execute a JSON-RPC method.

    Returns the JSON-RPC result dict from the method response.
    Raises AssertionError on unexpected HTTP status.
    """
    headers = dict(INIT_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    async with _lifespan(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # Step 1: initialize handshake
        init_resp = await c.post("/mcp", json=INIT_BODY, headers=headers)
        assert init_resp.status_code == 200, f"initialize failed: {init_resp.text}"
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        # Step 2: actual method call
        body = {"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}}
        resp = await c.post("/mcp", json=body, headers=headers)
        assert resp.status_code == 200, f"{method} failed ({resp.status_code}): {resp.text}"
        return _parse_jsonrpc_response(resp)


def _parse_jsonrpc_response(resp: httpx.Response) -> dict[str, object]:
    """Parse a JSON-RPC response from either JSON or SSE format."""
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        # SSE: parse the data line
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])  # type: ignore[return-value]
        raise AssertionError(f"No data line in SSE response: {resp.text!r}")
    else:
        return resp.json()  # type: ignore[return-value]


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
        admin_account = Account(tenant_id=tenant.id, role="admin")
        s.add(admin_account)
        await s.flush()
        user_account = Account(tenant_id=tenant.id, role="user")
        s.add(user_account)
        await s.flush()
        admin_token = make_jwt(account_id=admin_account.id)
        user_token = make_jwt(account_id=user_account.id)
    return admin_token, user_token


async def test_non_admin_list_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Non-admin tools/list returns only search_tools, call_tool, list_credentials."""
    admin_token, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await _mcp_session(app, token=user_token, method="tools/list")
    tools_payload = result.get("result", result)
    tool_names: list[str] = [t["name"] for t in tools_payload.get("tools", [])]  # type: ignore[union-attr]

    # Only meta-tools visible to non-admins
    for expected in ("search_tools", "call_tool", "list_credentials"):
        assert expected in tool_names, (
            f"Expected {expected!r} in non-admin tool list, got: {tool_names}"
        )

    # Admin tools must NOT appear. Every gated mutating tool is now admin-tagged
    # (D-08 sweep). Reads (list_agents, get_agent, list_skills, get_skill, etc.)
    # stay visible to all sessions.
    admin_tool_names = {
        # agents.py mutating tools
        "create_agent",
        "update_agent",
        "attach_mcp_server",
        "fork_agent",
        "archive_agent",
        # skills.py mutating tools
        "sync_skills",
        "skills_sync",
        "delete_skill",
        "skills_delete",
        # self_edit.py mutating tools
        "self_write_file",
        "self_delete_file",
        "set_repo_binding",
        "clear_repo_binding",
        # environments.py mutating tools (reads are untagged + ungated,
        # matching agents/skills — D-08 tag/gate agreement)
        "create_environment",
        "update_environment",
        "archive_environment",
    }
    for admin_tool in admin_tool_names:
        assert admin_tool not in tool_names, (
            f"Admin tool {admin_tool!r} visible to non-admin, tool_names={tool_names}"
        )


async def test_non_admin_search_excludes_admin_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Non-admin search_tools('archive agent') must not surface archive_agent."""
    admin_token, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await _mcp_session(
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

    After the D-08 tag sweep every gated mutating tool carries tags={'admin'},
    so fastmcp's get_tool filters them for non-admin sessions before any impl
    runs. The D-28 impl gate remains as defense-in-depth."""
    admin_token, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await _mcp_session(
        app,
        token=user_token,
        method="tools/call",
        params={
            "name": "call_tool",
            "arguments": {
                "tool_name": "archive_agent",
                "tool_args": {"name": "daimon"},
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


async def test_non_admin_search_excludes_fork_agent(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """fork_agent is now admin-tagged (D-08 sweep) — non-admin search must not surface it.

    The D-28 impl gate remains as defense-in-depth, but the visibility layer
    hides it from non-admin sessions before they can call it."""
    _, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await _mcp_session(
        app,
        token=user_token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": "fork agent"}},
    )
    call_result = result.get("result", result)
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    assert "fork_agent" not in output_text, (
        f"fork_agent must NOT be discoverable by non-admin; got: {output_text!r}"
    )


async def test_non_admin_search_excludes_skills_sync(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """skills_sync is now admin-tagged (D-08 sweep) — non-admin search must not surface it.

    The D-28 impl gate remains as defense-in-depth, but the visibility layer
    hides it from non-admin sessions before they can call it."""
    _, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await _mcp_session(
        app,
        token=user_token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": "sync skills"}},
    )
    call_result = result.get("result", result)
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    assert "skills_sync" not in output_text, (
        f"skills_sync must NOT be discoverable by non-admin; got: {output_text!r}"
    )


async def test_discord_vault_token_is_admin_claim_without_internal_denied_admin_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Phase 88-03 (#162): a Discord vault token with is_admin=True but no internal claim
    must NOT gain admin tool visibility.

    The old behavior (pre-88-03) elevated guild admins via is_admin alone. That was the
    RBAC escalation bug: a stale pre-sweep Discord vault credential baked with is_admin=True
    could ride a non-admin caller's session into admin tooling. Closed by requiring the
    internal discriminator claim (emitted only by mint_internal_mcp_token)."""
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="guild-isadmin-test")
        user_account = Account(tenant_id=tenant.id, role="user")
        s.add(user_account)
        await s.flush()
        # Discord vault token: is_admin=True but no internal claim
        guild_admin_token = make_jwt(account_id=user_account.id, is_admin=True)
    app = _make_app(sessionmaker)

    result = await _mcp_session(
        app,
        token=guild_admin_token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": "sync skills"}},
    )
    call_result = result.get("result", result)
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    assert "sync_skills" not in output_text, (
        f"Discord vault token with is_admin but no internal must NOT see sync_skills; got: {output_text!r}"
    )


async def test_admin_search_includes_admin_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Admin search_tools('create agent') returns create_agent in results."""
    admin_token, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await _mcp_session(
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
    """Admin search_tools('fork') returns fork_agent — proves IdentityMiddleware
    enable_components re-enables newly-tagged tools for admin sessions."""
    admin_token, _ = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await _mcp_session(
        app,
        token=admin_token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": "fork agent"}},
    )
    call_result = result.get("result", result)
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    assert "fork_agent" in output_text, (
        f"fork_agent must be discoverable by admin after D-08 tag sweep; got: {output_text!r}"
    )


async def test_admin_list_tools_returns_meta_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Admin tools/list returns the BM25 meta-tools (search_tools, call_tool, list_credentials).

    The BM25SearchTransform collapses the full tool catalog into meta-tools for
    all users. Admin tools are accessible via search_tools/call_tool (already
    tested in test_admin_search_includes_admin_tools). The tools/list response
    is identical for admin and non-admin in terms of visible tool names — the
    difference is that admin call_tool calls against admin tools are allowed.
    """
    admin_token, user_token = await _seed_admin_and_user(sessionmaker)
    app = _make_app(sessionmaker)

    result = await _mcp_session(app, token=admin_token, method="tools/list")
    tools_payload = result.get("result", result)
    tool_names: list[str] = [t["name"] for t in tools_payload.get("tools", [])]  # type: ignore[union-attr]

    for expected in ("search_tools", "call_tool", "list_credentials"):
        assert expected in tool_names, (
            f"Expected meta-tool {expected!r} missing from admin tool list: {tool_names}"
        )
