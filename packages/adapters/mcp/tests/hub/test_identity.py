"""HubIdentityMiddleware turns proxy token claims into a HubIdentity in request state."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from daimon.adapters.mcp.hub.claims import decode_hub_claims, encode_hub_claims
from daimon.adapters.mcp.hub.identity import HubIdentity, HubIdentityMiddleware, _hub_auth
from daimon.core.hub_identity import HubTenant
from fastmcp import Context, FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.types import ASGIApp, Message

pytestmark = pytest.mark.asyncio

_TENANT = HubTenant(
    tenant_id=uuid.uuid4(), account_id=uuid.uuid4(), workspace_id="g1", workspace_name="PyMC"
)


def test_claims_round_trip() -> None:
    claims = encode_hub_claims(platform="discord", platform_user_id="u1", tenants=[_TENANT])
    identity = decode_hub_claims({"sub": "u1", "upstream_claims": claims})
    assert identity == HubIdentity(platform="discord", platform_user_id="u1", tenants=(_TENANT,)), (
        f"round trip changed the identity: {identity!r}"
    )


def test_decode_returns_none_without_upstream_claims() -> None:
    assert decode_hub_claims({"sub": "u1"}) is None, "missing upstream_claims must decode to None"


def test_decode_returns_none_on_malformed_tenant() -> None:
    bad = {"platform": "discord", "platform_user_id": "u1", "tenants": [{"tenant_id": "nope"}]}
    assert decode_hub_claims({"upstream_claims": bad}) is None, "malformed tenant must fail closed"


def _app_with_tokens(tokens: dict[str, dict[str, object]]) -> FastMCP:
    mcp = FastMCP(name="hub-test", auth=StaticTokenVerifier(tokens=tokens))
    mcp.add_middleware(HubIdentityMiddleware())

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        return (await _hub_auth(ctx)).platform_user_id

    return mcp


# ---------------------------------------------------------------------------
# HTTP harness (FastMCP's in-memory Client transport does not support auth;
# same pattern as tests/tools/test_agent_chat.py:200-283).
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _lifespan(app: ASGIApp) -> AsyncIterator[None]:
    send_q: asyncio.Queue[Message] = asyncio.Queue()
    recv_q: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return await recv_q.get()

    async def send(message: Message) -> None:
        await send_q.put(message)

    async def run() -> None:
        await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    task = asyncio.create_task(run())
    await recv_q.put({"type": "lifespan.startup"})
    msg = await send_q.get()
    assert msg["type"] == "lifespan.startup.complete", msg
    try:
        yield
    finally:
        await recv_q.put({"type": "lifespan.shutdown"})
        msg = await send_q.get()
        assert msg["type"] == "lifespan.shutdown.complete", msg
        await task


def _parse_jsonrpc(resp: httpx.Response) -> dict[str, object]:
    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct:
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])  # type: ignore[return-value]
        raise AssertionError(f"No data line in SSE: {resp.text!r}")
    return resp.json()  # type: ignore[return-value]


async def _call_whoami_via_http(app: ASGIApp, token: str) -> dict[str, object]:
    """Initialize an MCP HTTP session and call tools/call for whoami; return the JSON-RPC result."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    async with _lifespan(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        init_resp = await c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            headers=headers,
        )
        assert init_resp.status_code == 200, f"initialize failed: {init_resp.text}"
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        call_resp = await c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            },
            headers=headers,
        )
        assert call_resp.status_code == 200, f"tools/call failed: {call_resp.text}"
        return _parse_jsonrpc(call_resp)


async def test_middleware_exposes_identity_to_tools() -> None:
    claims = encode_hub_claims(platform="slack", platform_user_id="U1", tenants=[_TENANT])
    mcp = _app_with_tokens({"tok": {"sub": "U1", "client_id": "c", "upstream_claims": claims}})
    result = await _call_whoami_via_http(mcp.http_app(), "tok")
    payload = result.get("result", result)
    assert isinstance(payload, dict), f"unexpected tools/call shape: {result!r}"
    assert not payload.get("isError"), (
        f"whoami should succeed for a hub-claims token; got {payload!r}"
    )
    content = payload.get("content") or []
    text = content[0]["text"] if content else None  # type: ignore[index]
    assert text == "U1", f"tool must see the login identity, got {payload!r}"


async def test_middleware_rejects_token_without_hub_claims() -> None:
    mcp = _app_with_tokens({"tok": {"sub": "U1", "client_id": "c"}})
    result = await _call_whoami_via_http(mcp.http_app(), "tok")
    # The middleware raises AuthorizationError before the tool is dispatched, so
    # the request fails at the JSON-RPC level rather than surfacing as a tool
    # result with isError=True.
    assert "error" in result, f"token without hub claims must be rejected; got {result!r}"
