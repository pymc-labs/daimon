"""Probe FastMCP HTTP app under httpx.ASGITransport — the Phase-2 test strategy.

Questions:
1. Can a `fastmcp.Client` driven via `httpx.ASGITransport` complete init +
   tool call against an in-process FastMCP HTTP app?
2. Does a custom HTTP middleware (for JWT verification at the HTTP layer,
   before MCP protocol entry) work the same way under ASGITransport and
   real uvicorn?
3. What does an HTTP-middleware 401 look like to the MCP client?

Run:
    uv run --with fastmcp --with pyjwt python scripts/probes/mcp/fastmcp_asgi_transport.py
"""

from __future__ import annotations

import asyncio

import httpx
from asgi_lifespan import LifespanManager
from fastmcp import Client, Context, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class BearerMiddleware(BaseHTTPMiddleware):
    """HTTP-layer auth: reject missing bearer before MCP handshake."""

    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/mcp"):
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer probe-token-"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            # Stash sub on scope for the MCP handler to read.
            request.scope["auth_sub"] = auth.removeprefix("Bearer probe-token-")
        return await call_next(request)


def build_app() -> tuple[FastMCP, object]:
    mcp = FastMCP("probe-http")

    @mcp.tool
    async def whoami(ctx: Context) -> dict:
        # Pull from ASGI scope via request_context.
        scope = ctx.request_context.request.scope if ctx.request_context.request else {}
        return {"sub_from_scope": scope.get("auth_sub")}

    http_app = mcp.http_app(
        path="/mcp",
        middleware=[],  # ASGIMiddleware list; we attach via starlette wrapper below
    )
    # Wrap with BearerMiddleware via starlette user_middleware mechanism
    http_app.add_middleware(BearerMiddleware)
    return mcp, http_app


async def probe_asgi_transport() -> None:
    print("\n===== ASGITransport: MCP Client over in-process app =====")
    _mcp, http_app = build_app()
    async with LifespanManager(http_app) as manager:
        app = manager.app
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://probe", timeout=10.0
        ) as http:
            await _body(http, transport)


async def _body(http, transport) -> None:
    if True:
        # First: confirm auth rejection path.
        r = await http.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
        print(f"[no auth] status={r.status_code} body={r.text[:120]}")

        # Now drive fastmcp.Client through the same transport with auth header.
        mcp_transport = StreamableHttpTransport(
            url="http://probe/mcp",
            headers={"Authorization": "Bearer probe-token-cli:testuser"},
            httpx_client_factory=lambda **kw: httpx.AsyncClient(
                transport=transport, base_url="http://probe", timeout=10.0, **{k: v for k, v in kw.items() if k not in ("base_url", "transport")}
            ),
        )
        async with Client(mcp_transport) as client:
            tools = await client.list_tools()
            print(f"[authed] tools: {[t.name for t in tools]}")
            result = await client.call_tool("whoami", {})
            print(f"[authed] whoami result: structured={result.structured_content}")


async def main() -> None:
    await probe_asgi_transport()
    print("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
