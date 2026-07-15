"""Probe: append custom HTTP routes to the Starlette app returned by FastMCP.http_app().

Questions:
1. Can we append Route("/healthz", ...) to app.router.routes so that both
   /healthz (custom) and the MCP endpoint (/mcp by default) work?
2. Which path does FastMCP serve JSON-RPC on by default?
3. Does appending routes after http_app() construction break anything?

Run:
    uv run --with 'fastmcp>=3.2.4,<4' --with httpx --with asgi-lifespan \
        python scripts/probes/mcp/healthz_route_append.py
"""

from __future__ import annotations

import asyncio
import json

import httpx
from asgi_lifespan import LifespanManager
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route


def build_app() -> object:
    mcp = FastMCP("probe")

    @mcp.tool
    def ping() -> str:
        return "pong"

    app = mcp.http_app()

    # --- PROBE: append custom routes after http_app() returns ---
    # Method 1: direct list append
    app.router.routes.append(
        Route("/healthz", lambda req: PlainTextResponse("ok"), methods=["GET"])
    )
    # Method 2: app.add_route (calls router.add_route under the hood)
    app.add_route(
        "/readyz",
        lambda req: PlainTextResponse("ready"),
        methods=["GET"],
    )

    return app


async def main() -> None:
    print("===== healthz_route_append probe =====")
    print()

    app = build_app()
    print("Routes registered on app.router.routes:")
    for r in app.router.routes:
        print(f"  {r}")
    print()

    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://probe", timeout=10.0
        ) as client:

            # --- 1. Custom /healthz route (appended via router.routes.append) ---
            r = await client.get("/healthz")
            healthz_ok = r.status_code == 200 and r.text == "ok"
            print(
                f"GET /healthz → {r.status_code} {r.text!r}  "
                f"{'PASS' if healthz_ok else 'FAIL'}"
            )

            # --- 2. Custom /readyz route (appended via app.add_route) ---
            r = await client.get("/readyz")
            readyz_ok = r.status_code == 200 and r.text == "ready"
            print(
                f"GET /readyz  → {r.status_code} {r.text!r}  "
                f"{'PASS' if readyz_ok else 'FAIL'}"
            )

            # --- 3. MCP JSON-RPC endpoint discovery ---
            # FastMCP default path is /mcp; confirm by sending a well-formed
            # initialize request and checking we get JSON-RPC back (not 404).
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
            mcp_at_mcp = r.status_code not in (404, 405)
            print(
                f"POST /mcp   → {r.status_code}  "
                f"{'PASS (MCP endpoint active)' if mcp_at_mcp else 'FAIL (404/405 — wrong path)'}"
            )
            if r.status_code == 200:
                # Streamable-HTTP may return SSE or JSON; just check it's not
                # a plain 404 page.
                preview = r.text[:200].replace("\n", " ")
                print(f"           body preview: {preview!r}")
            elif r.status_code not in (404, 405):
                # Accept-level negotiation or session error — still proves
                # the route exists.
                try:
                    body = r.json()
                    print(f"           json body: {json.dumps(body)[:200]}")
                except Exception:
                    print(f"           text: {r.text[:200]!r}")

            # Confirm /mcp is the ONLY MCP path (/ should 404).
            r_root = await client.post(
                "/",
                json=init_payload,
                headers={"Accept": "application/json, text/event-stream"},
            )
            print(
                f"POST /      → {r_root.status_code}  "
                f"{'(no MCP at root — as expected)' if r_root.status_code == 404 else '(MCP also at root — note this)'}"
            )

    print()
    all_pass = healthz_ok and readyz_ok and mcp_at_mcp
    print("===== RESULT =====")
    print(f"Route append works:          {'PASS' if healthz_ok and readyz_ok else 'FAIL'}")
    print(f"MCP endpoint alive at /mcp:  {'PASS' if mcp_at_mcp else 'FAIL'}")
    print(f"Overall:                     {'PASS' if all_pass else 'FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())
