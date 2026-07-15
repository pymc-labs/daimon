"""Probe how MA vault `static_bearer` credentials are shaped + injected.

The phase-2 MCP server design assumes:
  - Daimon mints a JWT, writes it as a `static_bearer` vault credential tied
    to an agent, with `mcp_server_url` pointing at daimon-mcp.
  - At turn time, MA injects `Authorization: Bearer <token>` when calling our
    MCP server's /mcp endpoint.

Questions:
  A. What is the create shape for a vault credential? (field names, content-types)
  B. What fields does `vault list` return — specifically which fields are
     safe-to-expose vs secret.
  C. When MA calls our MCP server, what headers does it actually send?

Strategy:
  1. Sweep POST /v1/vaults with plausible shapes to characterize the API.
  2. If vault create succeeds, GET /v1/vaults to see list shape.
  3. Spin up a local HTTP capture server on 127.0.0.1:<port>. Expose it via
     a tunnel-free path — not possible from MA's public internet side.

Note on (3): MA can only call publicly-reachable URLs. From a laptop there's
no way to have MA hit our capture server without a tunnel. This probe
*characterizes the API shape* (A+B); the "what-headers-does-MA-send" question
requires a deployed daimon-mcp or an ngrok/cloudflared tunnel and is noted
but not run here. See scripts/probes/mcp/fastmcp_factory_uvicorn.py for the
server-side shape; the missing piece is confirming MA's actual outbound
header format, which we'll verify once daimon-mcp is deployed in staging.

Run:
    uv run python scripts/probes/managed_agents/mcp_vault_injection.py
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import httpx
from dotenv import load_dotenv

API_BASE = "https://api.anthropic.com/v1"
BETA = "managed-agents-2026-04-01"


def hdrs() -> dict[str, str]:
    return {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "anthropic-beta": BETA,
        "content-type": "application/json",
    }


async def try_post(http: httpx.AsyncClient, path: str, body: dict) -> None:
    print(f"\nPOST {path}")
    print(f"  body: {json.dumps(body)[:200]}")
    r = await http.post(f"{API_BASE}{path}", headers=hdrs(), json=body)
    txt = r.text
    if len(txt) > 400:
        txt = txt[:400] + f"... (trunc, len={len(r.text)})"
    print(f"  {r.status_code}: {txt}")
    if r.status_code < 300:
        return r.json()
    return None


async def try_get(http: httpx.AsyncClient, path: str) -> None:
    print(f"\nGET {path}")
    r = await http.get(f"{API_BASE}{path}", headers=hdrs())
    txt = r.text
    if len(txt) > 800:
        txt = txt[:800] + f"... (trunc)"
    print(f"  {r.status_code}: {txt}")
    if r.status_code < 300:
        return r.json()
    return None


async def main() -> None:
    load_dotenv()
    async with httpx.AsyncClient(timeout=30.0) as http:
        # 1. GET current vaults (baseline list shape).
        await try_get(http, "/vaults")
        await try_get(http, "/vault_credentials")

        # 2. Create shapes — iterate plausible envelopes.
        suffix = uuid.uuid4().hex[:6]

        # Shape A: direct credential create.
        await try_post(
            http,
            "/vault_credentials",
            {
                "type": "static_bearer",
                "mcp_server_url": f"https://probe-{suffix}.example.com/mcp",
                "token": f"probe-token-{suffix}",
            },
        )

        # Shape B: auth envelope.
        await try_post(
            http,
            "/vault_credentials",
            {
                "auth": {
                    "type": "static_bearer",
                    "token": f"probe-token-{suffix}",
                    "mcp_server_url": f"https://probe-{suffix}.example.com/mcp",
                },
            },
        )

        # Shape C: /vaults plural.
        await try_post(
            http,
            "/vaults",
            {
                "type": "static_bearer",
                "mcp_server_url": f"https://probe-{suffix}.example.com/mcp",
                "token": f"probe-token-{suffix}",
            },
        )

        # Shape D: nested under /vaults.
        await try_post(
            http,
            "/vaults",
            {
                "auth": {
                    "type": "static_bearer",
                    "token": f"probe-token-{suffix}",
                },
                "mcp_server_url": f"https://probe-{suffix}.example.com/mcp",
            },
        )

        # 3. List after creates to observe shape.
        await try_get(http, "/vaults")

    print("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
