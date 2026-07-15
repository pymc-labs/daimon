"""Sweep POST /v1/vaults to characterize the vault create schema.

Follow-up to mcp_vault_injection.py which revealed:
  - `/vaults` is the endpoint (not `/vault_credentials`).
  - `mcp_server_url` and `auth` are *not* valid top-level fields.

So: what IS the shape? Sweep plausible alternatives.

Run:
    uv run python scripts/probes/managed_agents/mcp_vault_shape_sweep.py
"""

from __future__ import annotations

import asyncio
import json
import os

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


async def try_post(http: httpx.AsyncClient, body: dict) -> None:
    print(f"\nPOST /vaults")
    print(f"  body: {json.dumps(body)}")
    r = await http.post(f"{API_BASE}/vaults", headers=hdrs(), json=body)
    txt = r.text
    if len(txt) > 300:
        txt = txt[:300] + "..."
    print(f"  {r.status_code}: {txt}")


async def main() -> None:
    load_dotenv()
    async with httpx.AsyncClient(timeout=30.0) as http:
        # Empty body — what's required?
        await try_post(http, {})

        # Just name.
        await try_post(http, {"name": "probe"})

        # Discover via different type discriminators.
        await try_post(http, {"type": "static_bearer"})
        await try_post(http, {"type": "bearer"})
        await try_post(http, {"type": "mcp"})
        await try_post(http, {"type": "oauth"})

        # Nested `credential` field?
        await try_post(
            http,
            {"credential": {"type": "static_bearer", "token": "t"}},
        )

        # Nested `value`?
        await try_post(http, {"value": {"token": "t"}})

        # `credentials` (array)?
        await try_post(http, {"credentials": [{"type": "static_bearer", "token": "t"}]})

        # Kind + data?
        await try_post(http, {"kind": "static_bearer", "token": "t"})

        # Just a token + server_url?
        await try_post(http, {"token": "t", "server_url": "https://x"})

        # url instead of server_url?
        await try_post(http, {"token": "t", "url": "https://x"})

        # Try GET /vaults with options to hint at schema
        print("\nGET /vaults?limit=1")
        r = await http.get(f"{API_BASE}/vaults", headers=hdrs(), params={"limit": 1})
        print(f"  {r.status_code}: {r.text[:400]}")

        # Is it nested under agents? POST /v1/agents/{id}/vaults? First list agents.
        r = await http.get(f"{API_BASE}/agents", headers=hdrs(), params={"limit": 1})
        print(f"\nGET /agents limit=1: {r.status_code} {r.text[:200]}")

        # Try OPTIONS on /vaults — some APIs describe.
        r = await http.request("OPTIONS", f"{API_BASE}/vaults", headers=hdrs())
        print(f"\nOPTIONS /vaults: {r.status_code} allow={r.headers.get('allow')} body={r.text[:200]}")

        # Try HEAD.
        r = await http.head(f"{API_BASE}/vaults", headers=hdrs())
        print(f"HEAD /vaults: {r.status_code}")

    print("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
