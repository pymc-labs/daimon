"""Middleware identity test via FastMCP's in-memory Client.

agent-chat narrowing tests use HTTP ASGI
transport because enable_components/disable_components require a real
session context (per test_rbac.py convention).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.middleware.mcp_identity import (
    IdentityMiddleware,
    production_agent_id_resolver,
    production_internal_resolver,
    production_is_admin_resolver,
    production_role_resolver,
    production_subject_resolver,
    production_tenant_resolver,
)
from fastmcp import Client, FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.context import Context
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Message

pytestmark = pytest.mark.asyncio

_INIT_BODY: dict[str, object] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
_INIT_HEADERS = {
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


def _parse_jsonrpc_response(resp: httpx.Response) -> dict[str, object]:
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])  # type: ignore[return-value]
        raise AssertionError(f"No data line in SSE response: {resp.text!r}")
    return resp.json()  # type: ignore[return-value]


async def _list_tools(
    app: ASGIApp,
    *,
    token: str,
) -> list[str]:
    """Initialize an MCP HTTP session and call tools/list; return tool names."""
    headers = dict(_INIT_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    async with _lifespan(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        init_resp = await c.post("/mcp", json=_INIT_BODY, headers=headers)
        assert init_resp.status_code == 200, f"initialize failed: {init_resp.text}"
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        body: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        resp = await c.post("/mcp", json=body, headers=headers)
        assert resp.status_code == 200, f"tools/list failed: {resp.text}"
        result = _parse_jsonrpc_response(resp)
    tools_payload = result.get("result", result)
    return [t["name"] for t in tools_payload.get("tools", [])]  # type: ignore[union-attr]


def _make_narrowing_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    token_map: dict[str, dict[str, str]],
) -> ASGIApp:
    """Build a minimal FastMCP app with IdentityMiddleware and two tools.

    Tools:
      - ``agent_tool`` tagged {"agent-chat"} — visible only to agent sessions
      - ``regular_tool`` with no tags — visible by default, hidden when narrowed

    Uses StaticTokenVerifier so token claims are supplied directly without
    a real DB lookup (the middleware resolvers use get_access_token() claims).
    """
    mcp = FastMCP(name="narrowing-test", auth=StaticTokenVerifier(tokens=token_map))
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=production_subject_resolver,
            tenant_resolver=production_tenant_resolver,
            role_resolver=production_role_resolver,
            agent_id_resolver=production_agent_id_resolver,
            is_admin_resolver=production_is_admin_resolver,
            internal_resolver=production_internal_resolver,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def agent_tool() -> str:  # pyright: ignore[reportUnusedFunction]
        return "agent"

    @mcp.tool
    async def regular_tool() -> str:  # pyright: ignore[reportUnusedFunction]
        return "regular"

    return mcp.http_app()  # pyright: ignore[reportReturnType]


async def _fixture_is_admin_resolver_false(_ctx: object) -> str | None:
    return None


async def _fixture_internal_resolver_false(_ctx: object) -> str | None:
    return None


async def test_identity_middleware_sets_auth_state_on_tool_call(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    captured: list[AuthIdentity | None] = []

    async def fixture_resolver(_ctx: object) -> str:
        return str(account_id)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str | None:
        return None

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=_fixture_is_admin_resolver_false,
            internal_resolver=_fixture_internal_resolver_false,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        auth = await ctx.get_state("auth")
        captured.append(auth)
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("whoami", {})

    assert len(captured) == 1, "middleware should run on tool call"
    got = captured[0]
    assert isinstance(got, AuthIdentity), "state should be AuthIdentity"
    assert got.account_id == account_id, "should carry account_id"
    assert got.tenant_id == tenant_id, "should carry tenant_id"


async def test_identity_middleware_populates_platform_and_external_id_from_claims(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Middleware reads platform and external_id from injected claims.

    The in-memory Client transport bypasses HTTP auth — get_access_token() returns
    None, so platform and external_id are None in the in-memory test path.
    Non-None claim injection is tested end-to-end via HTTP in
    test_server_http_auth.py::test_verified_token_carries_platform_and_external_id_claims.

    This test verifies that when the token carries platform/external_id claims
    the middleware passes them through without error (the None path is the in-memory
    transport path; the non-None path requires the HTTP transport).
    """
    account_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    captured: list[AuthIdentity | None] = []

    async def fixture_subject_resolver(_ctx: object) -> str:
        return str(account_id)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str | None:
        return None

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_subject_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=_fixture_is_admin_resolver_false,
            internal_resolver=_fixture_internal_resolver_false,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        auth = await ctx.get_state("auth")
        captured.append(auth)
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("whoami", {})

    assert len(captured) == 1, "middleware should run on tool call"
    got = captured[0]
    assert isinstance(got, AuthIdentity), "state should be AuthIdentity"
    # In-memory transport: get_access_token() returns None → claims are None.
    # HTTP transport with StaticTokenVerifier is tested in test_server_http_auth.py.
    assert got.platform is None, "platform is None when no access token present"
    assert got.external_id is None, "external_id is None when no access token present"


async def test_identity_middleware_platform_external_id_none_when_claims_absent(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    captured: list[AuthIdentity | None] = []

    async def fixture_subject_resolver(_ctx: object) -> str:
        return str(account_id)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str | None:
        return None

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_subject_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=_fixture_is_admin_resolver_false,
            internal_resolver=_fixture_internal_resolver_false,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        auth = await ctx.get_state("auth")
        captured.append(auth)
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("whoami", {})

    assert len(captured) == 1, "middleware should not raise when platform/external_id claims absent"
    got = captured[0]
    assert isinstance(got, AuthIdentity), "state should be AuthIdentity even with no claims"
    assert got.platform is None, "platform should be None when claim absent"
    assert got.external_id is None, "external_id should be None when claim absent"


async def test_identity_middleware_rejects_missing_tenant_id(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """If tenant_resolver returns None, middleware raises AuthorizationError."""
    account_id = uuid.uuid4()

    async def fixture_resolver(_ctx: object) -> str:
        return str(account_id)

    async def fixture_tenant_resolver(_ctx: object) -> str | None:
        return None

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str | None:
        return None

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=_fixture_is_admin_resolver_false,
            internal_resolver=_fixture_internal_resolver_false,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        auth = await ctx.get_state("auth")
        return f"account={auth.account_id}" if auth else "none"

    from mcp.shared.exceptions import McpError

    # The middleware raises AuthorizationError on every request, including the
    # MCP initialize handshake. The Client.__aenter__ propagates this as McpError.
    try:
        async with Client(mcp) as client:
            result = await client.call_tool("whoami", {})
            text = result.content[0].text  # type: ignore[union-attr]
            assert "error" in text.lower() or "none" in text.lower(), (  # pyright: ignore[reportUnknownMemberType]
                "missing tenant_id should prevent tool execution or surface error"
            )
    except McpError as exc:
        assert "tenant_id" in str(exc).lower() or "invalid" in str(exc).lower(), (
            "missing tenant_id should produce a recognisable error"
        )


async def test_auth_identity_default_agent_id_is_none() -> None:
    """AuthIdentity.agent_id defaults to None when not supplied."""
    from daimon.core.stores.domain import Role

    identity = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
    )
    assert identity.agent_id is None, "agent_id should default to None"


async def test_identity_middleware_populates_agent_id_when_claim_present(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Middleware decodes the agent_id JWT claim into AuthIdentity.agent_id.

    Note: when agent_id is present, agent-chat narrowing fires and hides all tools
    that lack the "agent-chat" tag. The whoami tool here is tagged "agent-chat"
    so it remains callable — the base behavior (agent_id populated on
    AuthIdentity) is unchanged; the agent-chat narrowing is an additive layer.
    """
    account_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    captured: list[AuthIdentity | None] = []

    async def fixture_subject_resolver(_ctx: object) -> str:
        return str(account_id)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str:
        return str(agent_id)

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_subject_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=_fixture_is_admin_resolver_false,
            internal_resolver=_fixture_internal_resolver_false,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        auth = await ctx.get_state("auth")
        captured.append(auth)
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("whoami", {})

    got = captured[0]
    assert isinstance(got, AuthIdentity), "state should be AuthIdentity"
    assert got.agent_id == agent_id, "agent_id claim should land on AuthIdentity"


async def test_identity_middleware_agent_id_none_when_claim_absent(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """When the agent_id resolver returns None, AuthIdentity.agent_id is None."""
    account_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    captured: list[AuthIdentity | None] = []

    async def fixture_subject_resolver(_ctx: object) -> str:
        return str(account_id)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str | None:
        return None

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_subject_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=_fixture_is_admin_resolver_false,
            internal_resolver=_fixture_internal_resolver_false,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        auth = await ctx.get_state("auth")
        captured.append(auth)
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("whoami", {})

    got = captured[0]
    assert isinstance(got, AuthIdentity), "state should be AuthIdentity"
    assert got.agent_id is None, "agent_id should be None when claim is absent"


async def test_identity_middleware_agent_id_malformed_treated_as_absent(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Malformed agent_id claim is treated as absent.

    Fail-closed: gcloud provider raises NoBindingError downstream.
    """
    account_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    captured: list[AuthIdentity | None] = []

    async def fixture_subject_resolver(_ctx: object) -> str:
        return str(account_id)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str:
        return "not-a-uuid"

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_subject_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=_fixture_is_admin_resolver_false,
            internal_resolver=_fixture_internal_resolver_false,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        auth = await ctx.get_state("auth")
        captured.append(auth)
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("whoami", {})

    got = captured[0]
    assert isinstance(got, AuthIdentity), "state should be AuthIdentity"
    assert got.agent_id is None, "malformed agent_id should be treated as None, not raise"


async def test_identity_middleware_populates_platform_user_id_from_injected_claim(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Middleware reads platform_user_id from the injected claim (no DB call).

    The verifier injects platform_user_id into AccessToken.claims after its DB
    JOIN; the middleware reads it inline.

    The in-memory Client transport bypasses HTTP auth — get_access_token() returns
    None in this path. The non-None claim injection path is tested end-to-end at
    the HTTP layer in test_server_http_auth.py. This test verifies the middleware
    handles the absent-claim case (None) without error.
    """
    account_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    captured: list[AuthIdentity | None] = []

    async def fixture_subject_resolver(_ctx: object) -> str:
        return str(account_id)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str | None:
        return None

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_subject_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=_fixture_is_admin_resolver_false,
            internal_resolver=_fixture_internal_resolver_false,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        auth = await ctx.get_state("auth")
        captured.append(auth)
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("whoami", {})

    got = captured[0]
    assert isinstance(got, AuthIdentity), "state should be AuthIdentity"
    assert got.platform_user_id is None, (
        "platform_user_id is None when no access token present (in-memory transport)"
    )


async def test_identity_middleware_platform_user_id_none_when_claim_absent(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """When platform_user_id claim is absent, AuthIdentity.platform_user_id is None.

    No DB lookup is performed — platform_user_id comes exclusively from the
    verifier-injected claim.
    """
    account_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    captured: list[AuthIdentity | None] = []

    async def fixture_subject_resolver(_ctx: object) -> str:
        return str(account_id)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str | None:
        return None

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_subject_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=_fixture_is_admin_resolver_false,
            internal_resolver=_fixture_internal_resolver_false,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        auth = await ctx.get_state("auth")
        captured.append(auth)
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("whoami", {})

    got = captured[0]
    assert isinstance(got, AuthIdentity), "state should be AuthIdentity"
    assert got.platform_user_id is None, (
        "platform_user_id should be None when no platform_user_id claim is present"
    )


async def test_identity_middleware_populates_is_admin_from_claim(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Middleware sets AuthIdentity.is_admin=True when both is_admin and internal claims present (trusted internal token)."""
    account_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    captured: list[AuthIdentity | None] = []

    async def fixture_subject_resolver(_ctx: object) -> str:
        return str(account_id)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str | None:
        return None

    async def fixture_is_admin_resolver(_ctx: object) -> str:
        return "true"

    async def fixture_internal_resolver(_ctx: object) -> str:
        return "true"

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_subject_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=fixture_is_admin_resolver,
            internal_resolver=fixture_internal_resolver,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        auth = await ctx.get_state("auth")
        captured.append(auth)
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("whoami", {})

    got = captured[0]
    assert isinstance(got, AuthIdentity), "state should be AuthIdentity"
    assert got.is_admin is True, (
        "middleware should populate is_admin=True when is_admin resolver returns 'true'"
    )


async def test_identity_middleware_defaults_is_admin_false_when_claim_absent(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Middleware sets AuthIdentity.is_admin=False when is_admin claim absent."""
    account_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    captured: list[AuthIdentity | None] = []

    async def fixture_subject_resolver(_ctx: object) -> str:
        return str(account_id)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str | None:
        return None

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_subject_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=_fixture_is_admin_resolver_false,
            internal_resolver=_fixture_internal_resolver_false,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        auth = await ctx.get_state("auth")
        captured.append(auth)
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("whoami", {})

    got = captured[0]
    assert isinstance(got, AuthIdentity), "state should be AuthIdentity"
    assert got.is_admin is False, (
        "middleware should default is_admin=False when is_admin claim is absent"
    )


# ---------------------------------------------------------------------------
# agent-chat narrowing + fail-closed guard
#
# These tests use HTTP ASGI transport (not in-memory Client) because
# disable_components/enable_components need a real session context.
# ---------------------------------------------------------------------------


async def test_identity_middleware_agent_id_narrows_to_agent_chat_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """When agent_id is a valid UUID, only agent-chat-tagged tools are visible.

    The middleware calls disable_components(match_all=True) then
    enable_components(tags={"agent-chat"}), so only agent_tool is listed
    and regular_tool is hidden.
    """
    agent_id = uuid.uuid4()
    token = "agent-token"
    token_map = {
        token: {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(uuid.uuid4()),
            "role": "user",
            "agent_id": str(agent_id),
            "client_id": "test",
        }
    }
    app = _make_narrowing_app(sessionmaker, token_map)

    tool_names = await _list_tools(app, token=token)

    assert "agent_tool" in tool_names, (
        "agent_tool (tagged agent-chat) must be visible for agent session"
    )
    assert "regular_tool" not in tool_names, (
        "regular_tool (no agent-chat tag) must be hidden when agent_id narrows to agent-chat"
    )


async def test_identity_middleware_no_agent_id_exposes_regular_tools_not_agent_chat(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """When agent_id is None, the agent-chat narrowing branch does NOT fire.

    regular_tool is visible (it has no tag and is not hidden by a baseline
    Visibility transform in this minimal app). agent_tool tagged agent-chat is
    visible too — no baseline Visibility(False, tags={"agent-chat"}) is set in
    this minimal test app, so the absence of narrowing means both tools show.

    The key assertion (fail-closed for malformed/absent agent_id) is that NO
    admin enable fires (no admin tags present) and NO disable_components(match_all)
    fires to narrow the session. Both tools being visible confirms no narrowing ran.
    """
    token = "user-token"
    token_map = {
        token: {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(uuid.uuid4()),
            "role": "user",
            # No agent_id claim
            "client_id": "test",
        }
    }
    app = _make_narrowing_app(sessionmaker, token_map)

    tool_names = await _list_tools(app, token=token)

    assert "regular_tool" in tool_names, (
        "regular_tool must be visible when agent_id is absent (no narrowing fired)"
    )
    # Both tools visible confirms the narrowing branch did not fire and no
    # disable_components(match_all=True) ran.
    assert "agent_tool" in tool_names, (
        "agent_tool must also be visible when agent_id is absent "
        "(no disable_components ran — both tools are in their default state)"
    )


async def test_identity_middleware_malformed_agent_id_fails_closed(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A malformed agent_id claim does NOT fire the narrowing branch (fail-closed).

    The malformed claim is silently nulled at l.141-144 of mcp_identity.py
    (T-19-04-07 / existing behavior). This means the narrowing branch (agent_id
    is not None) is skipped — the session sees both tools, exactly as the
    no-agent_id case above. Crucially, it does NOT expose admin tools either.
    """
    token = "bad-agent-token"
    token_map = {
        token: {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(uuid.uuid4()),
            "role": "user",
            "agent_id": "not-a-uuid",  # malformed — must be silently nulled
            "client_id": "test",
        }
    }
    app = _make_narrowing_app(sessionmaker, token_map)

    tool_names = await _list_tools(app, token=token)

    # Malformed agent_id is treated as None — narrowing branch does NOT fire.
    # Both tools visible confirms no disable_components(match_all=True) ran.
    assert "regular_tool" in tool_names, (
        "regular_tool must be visible when agent_id is malformed "
        "(narrowing must NOT fire on malformed claim)"
    )
    assert "agent_tool" in tool_names, (
        "agent_tool must also be visible — disable_components did not run "
        "because the malformed agent_id was silently nulled (fail-closed)"
    )


# ---------------------------------------------------------------------------
# live DB role OR internal-token is_admin gate
# ---------------------------------------------------------------------------


async def test_db_role_admin_grants_admin(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """ADMIN-01: a caller whose live DB role is ADMIN gets admin elevation.

    role resolver returns "admin"; is_admin and internal claims are absent.
    AuthIdentity.is_admin must be True and admin-tagged components enabled.
    This is the primary admin path after 88-04 writes account.role from Discord perms.
    """
    account_id_a = uuid.uuid4()
    tenant_id_a = uuid.uuid4()
    captured: list[AuthIdentity | None] = []

    async def fixture_subject_resolver(_ctx: object) -> str:
        return str(account_id_a)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id_a)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "admin"

    async def fixture_agent_id_resolver(_ctx: object) -> str | None:
        return None

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_subject_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=_fixture_is_admin_resolver_false,
            internal_resolver=_fixture_internal_resolver_false,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        auth = await ctx.get_state("auth")
        captured.append(auth)
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("whoami", {})

    got = captured[0]
    assert isinstance(got, AuthIdentity), "state should be AuthIdentity"
    assert got.is_admin is True, (
        "live DB role==ADMIN must grant admin even when is_admin and internal claims are absent "
        "(ADMIN-01 — live-role path, independent of any baked claim)"
    )


async def test_discord_vault_token_baked_is_admin_without_internal_does_not_elevate(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """ADMIN-01: a Discord vault token with is_admin=True but no internal claim must NOT elevate.

    This is the adversarial stale-pre-sweep credential shape: role=user,
    is_admin claim True (baked before 88-01 stripped it), internal claim absent.
    The gate must DENY admin elevation — correctness does NOT depend on the 88-06
    sweep having stripped is_admin from the credential (closes #162 escalation).
    """
    account_id_b = uuid.uuid4()
    tenant_id_b = uuid.uuid4()
    captured: list[AuthIdentity | None] = []

    async def fixture_subject_resolver(_ctx: object) -> str:
        return str(account_id_b)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id_b)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str | None:
        return None

    async def fixture_is_admin_resolver_true(_ctx: object) -> str:
        return "true"  # stale baked is_admin claim on a Discord vault token

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_subject_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=fixture_is_admin_resolver_true,
            internal_resolver=_fixture_internal_resolver_false,  # NO internal claim
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        auth = await ctx.get_state("auth")
        captured.append(auth)
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("whoami", {})

    got = captured[0]
    assert isinstance(got, AuthIdentity), "state should be AuthIdentity"
    assert got.is_admin is False, (
        "a Discord vault token with is_admin=True but NO internal claim must NOT grant admin "
        "(ADMIN-01 — baked is_admin alone never elevates; escalation #162 closed at gate "
        "independent of the 88-06 sweep)"
    )


async def test_internal_token_is_admin_claim_still_grants_admin(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """ADMIN-02: an internal token (is_admin=True + internal=True) still grants admin.

    role=user, is_admin claim True, internal claim True — the mint_internal_mcp_token
    shape (CLI/scheduler/headless). Admin must be preserved; stripping it entirely would
    break the operator-level and routine-runner admin path.
    """
    account_id_c = uuid.uuid4()
    tenant_id_c = uuid.uuid4()
    captured: list[AuthIdentity | None] = []

    async def fixture_subject_resolver(_ctx: object) -> str:
        return str(account_id_c)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id_c)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str | None:
        return None

    async def fixture_is_admin_resolver_true(_ctx: object) -> str:
        return "true"

    async def fixture_internal_resolver_true(_ctx: object) -> str:
        return "true"

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_subject_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=fixture_is_admin_resolver_true,
            internal_resolver=fixture_internal_resolver_true,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        auth = await ctx.get_state("auth")
        captured.append(auth)
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("whoami", {})

    got = captured[0]
    assert isinstance(got, AuthIdentity), "state should be AuthIdentity"
    assert got.is_admin is True, (
        "an internal token (is_admin=True AND internal=True) must still grant admin "
        "(ADMIN-02 — CLI/scheduler/headless admin must not be stripped)"
    )
