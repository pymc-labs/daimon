"""Model of the MA vault: create a vault container, probe credential sub-resources.

Prior probes established:
  - POST /vaults requires `display_name` and nothing else.
  - `type`, `auth`, `mcp_server_url`, `token`, etc. are NOT fields on /vaults.

So a vault must be a container. Credentials live nested. Find the path.

Run:
    uv run python scripts/probes/managed_agents/mcp_vault_model.py
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


async def main() -> None:
    load_dotenv()
    async with httpx.AsyncClient(timeout=30.0) as http:
        suffix = uuid.uuid4().hex[:6]

        print("=== 1. Create vault container ===")
        r = await http.post(
            f"{API_BASE}/vaults",
            headers=hdrs(),
            json={"display_name": f"probe-vault-{suffix}"},
        )
        print(f"  {r.status_code}: {r.text[:400]}")
        if r.status_code >= 300:
            return
        vault = r.json()
        vault_id = vault["id"]
        print(f"  vault full: {json.dumps(vault, indent=2)}")

        print("\n=== 2. Retrieve vault ===")
        r = await http.get(f"{API_BASE}/vaults/{vault_id}", headers=hdrs())
        print(f"  {r.status_code}: {r.text[:400]}")

        print("\n=== 3. Probe credential sub-resources ===")
        for path in (
            f"/vaults/{vault_id}/credentials",
            f"/vaults/{vault_id}/tokens",
            f"/vaults/{vault_id}/entries",
            f"/vaults/{vault_id}/secrets",
            f"/vaults/{vault_id}/servers",
            f"/vaults/{vault_id}/mcp_servers",
        ):
            r = await http.get(f"{API_BASE}{path}", headers=hdrs())
            print(f"  GET {path} → {r.status_code} {r.text[:200]}")

        print("\n=== 4. Try POST-ing a credential to the likely sub-resource ===")
        # Try shapes on the plural paths that returned 200
        for path in (
            f"/vaults/{vault_id}/credentials",
            f"/vaults/{vault_id}/tokens",
        ):
            for body in (
                {"type": "static_bearer", "token": "probe-token", "mcp_server_url": "https://example.com/mcp"},
                {"auth": {"type": "static_bearer", "token": "probe-token"}, "mcp_server_url": "https://example.com/mcp"},
                {"display_name": "cred1"},
            ):
                r = await http.post(f"{API_BASE}{path}", headers=hdrs(), json=body)
                print(f"  POST {path} body={json.dumps(body)[:80]}")
                print(f"    → {r.status_code} {r.text[:250]}")

        print("\n=== 5. Vault in update — can agent reference a vault? ===")
        r = await http.get(f"{API_BASE}/vaults/{vault_id}", headers=hdrs())
        print(f"  vault post-experiments: {r.text[:400]}")

        print("\n=== 6. Cleanup (archive if supported) ===")
        for method in ("DELETE",):
            r = await http.request(method, f"{API_BASE}/vaults/{vault_id}", headers=hdrs())
            print(f"  {method} /vaults/{vault_id} → {r.status_code} {r.text[:150]}")
        r = await http.post(f"{API_BASE}/vaults/{vault_id}/archive", headers=hdrs())
        print(f"  POST /vaults/{vault_id}/archive → {r.status_code} {r.text[:150]}")

    print("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
