"""Nail down POST /v1/vaults/{id}/credentials schema.

Prior probe showed the error progression:
  {} → "auth: Field required"
  {"auth": {...}} → "auth.mcp_server_url: Field required"

So credential shape is `{auth: {mcp_server_url, ...}}`. Discover the rest.

Run:
    uv run python scripts/probes/managed_agents/mcp_vault_credential_create.py
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


async def post(http, path, body):
    r = await http.post(f"{API_BASE}{path}", headers=hdrs(), json=body)
    print(f"\nPOST {path} body={json.dumps(body)[:120]}")
    print(f"  {r.status_code}: {r.text[:350]}")
    return r


async def main() -> None:
    load_dotenv()
    async with httpx.AsyncClient(timeout=30.0) as http:
        suffix = uuid.uuid4().hex[:6]
        r = await http.post(
            f"{API_BASE}/vaults", headers=hdrs(), json={"display_name": f"probe-{suffix}"}
        )
        r.raise_for_status()
        vid = r.json()["id"]
        p = f"/vaults/{vid}/credentials"

        # Start minimal and let errors guide us.
        await post(http, p, {"auth": {"mcp_server_url": "https://x.example.com/mcp"}})
        await post(
            http, p, {"auth": {"mcp_server_url": "https://x.example.com/mcp", "type": "static_bearer"}}
        )
        await post(
            http,
            p,
            {
                "auth": {
                    "mcp_server_url": "https://x.example.com/mcp",
                    "type": "static_bearer",
                    "token": "probe-token",
                }
            },
        )
        # Common auth types to enumerate.
        for t in ("bearer", "oauth2", "oauth", "basic", "api_key", "mcp", "static"):
            await post(http, p, {"auth": {"mcp_server_url": "https://x.example.com/mcp", "type": t}})

        # What if we add a display name + metadata to the working shape?
        await post(
            http,
            p,
            {
                "display_name": "cred",
                "auth": {
                    "mcp_server_url": "https://x.example.com/mcp",
                    "type": "static_bearer",
                    "token": "probe-token",
                },
            },
        )

        # List credentials to see stored shape.
        r = await http.get(f"{API_BASE}{p}", headers=hdrs())
        print(f"\nGET {p}:\n  {r.status_code}: {r.text[:800]}")

        # Cleanup.
        await http.delete(f"{API_BASE}/vaults/{vid}", headers=hdrs())


if __name__ == "__main__":
    asyncio.run(main())
