"""Probe: does auth wrap appended /healthz routes when FastMCP is built with auth?

Questions:
1. GET /healthz (no Authorization) → 200 or 401?
2. GET /healthz (valid token)       → 200 or 401?
3. POST /mcp    (no Authorization)  → confirms auth is actually active

Mechanism insight (from source):
- FastMCP's create_streamable_http_app applies RequireAuthMiddleware as a
  per-route ASGI wrapper around only the /mcp endpoint route, not as a global
  Starlette middleware that intercepts all routes.
- AuthenticationMiddleware (from auth.get_middleware()) is global — it attempts
  to parse Bearer tokens on every request — but it only *populates* scope["user"];
  it does NOT reject unauthenticated requests. Rejection is done by
  RequireAuthMiddleware, which only wraps the specific MCP route.
- Therefore, any route appended via app.add_route() or app.router.routes.append()
  after http_app() returns sits alongside the MCP route without RequireAuthMiddleware
  and is therefore unauthenticated.

Run:
    uv run --with 'fastmcp>=3.2.4,<4' --with httpx --with asgi-lifespan \
        python scripts/probes/mcp/healthz_auth_gate.py
"""

from __future__ import annotations

import asyncio

import httpx
from asgi_lifespan import LifespanManager
from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import PlainTextResponse

VALID_TOKEN = "super-secret-probe-token"


def build_authed_app() -> object:
    verifier = StaticTokenVerifier(
        tokens={
            VALID_TOKEN: {
                "client_id": "probe-client",
                "scopes": [],
            }
        }
    )
    mcp = FastMCP(name="probe", auth=verifier)

    @mcp.tool
    def ping() -> str:
        return "pong"

    app = mcp.http_app()

    # Append /healthz after construction — the method under test
    async def healthz(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app.add_route("/healthz", healthz, methods=["GET"])
    return app


async def main() -> None:
    print("===== healthz_auth_gate probe =====\n")

    app = build_authed_app()

    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://probe", timeout=10.0
        ) as client:

            # 1. /healthz — no token
            r = await client.get("/healthz")
            healthz_no_token = r.status_code
            print(f"GET /healthz (no token)    → {healthz_no_token}")

            # 2. /healthz — valid token
            r = await client.get(
                "/healthz",
                headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            )
            healthz_with_token = r.status_code
            print(f"GET /healthz (valid token) → {healthz_with_token}")

            # 3. /mcp — no token (baseline: confirm auth is active)
            init_payload = {
                "jsonrpc": "2.0",
                "method": "initialize",
                "id": 1,
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "0"},
                },
            }
            r = await client.post(
                "/mcp",
                json=init_payload,
                headers={"Accept": "application/json, text/event-stream"},
            )
            mcp_no_token = r.status_code
            print(f"POST /mcp   (no token)     → {mcp_no_token}  (baseline: auth active?)")

    print()
    healthz_gated = healthz_no_token == 401
    auth_active = mcp_no_token == 401
    print("===== RESULT =====")
    print(f"/healthz is auth-gated:    {'YES' if healthz_gated else 'NO'}")
    print(f"Auth active on /mcp:       {'YES' if auth_active else 'NO (UNEXPECTED)'}")
    print()
    if not healthz_gated and auth_active:
        print(
            "CONCLUSION: appended routes are NOT auth-gated. "
            "RequireAuthMiddleware wraps only the /mcp route, not the whole app."
        )
        print(
            "RECOMMENDATION for create_mcp_app: "
            "use app.add_route('/healthz', handler) directly on the http_app() "
            "result — no exemption config needed; health routes are free by design."
        )
    elif healthz_gated:
        print(
            "CONCLUSION: appended routes ARE auth-gated. "
            "Mount health routes on a parent Starlette app instead."
        )
    else:
        print("UNEXPECTED: auth does not appear to be active on /mcp either.")


if __name__ == "__main__":
    asyncio.run(main())
