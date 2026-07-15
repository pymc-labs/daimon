"""Characterize whether MA vault credentials support in-place update.

Phase 37 needs to decide between three mitigation paths for stale credentials
held in existing warm-path vaults after the JWT shape changes:

  (a) In-place update — PATCH/PUT /v1/vaults/{vid}/credentials/{cid}
  (b) Delete + recreate — DELETE then POST (brief window with no credential)
  (c) New credential alongside — POST a second credential, leave old one

This probe enumerates which endpoints exist and what shapes they accept.

Run:
    uv run python scripts/probes/managed_agents/mcp_vault_credential_update.py
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


async def call(http: httpx.AsyncClient, method: str, path: str, body: object | None = None) -> httpx.Response:
    r = await http.request(method, f"{API_BASE}{path}", headers=hdrs(), json=body)
    label = f"{method} {path}"
    if body is not None:
        label += f" body={json.dumps(body)[:140]}"
    print(f"\n{label}")
    print(f"  {r.status_code}: {r.text[:400]}")
    return r


async def main() -> None:
    load_dotenv()
    async with httpx.AsyncClient(timeout=30.0) as http:
        suffix = uuid.uuid4().hex[:6]

        # Create a vault.
        r = await call(http, "POST", "/vaults", {"display_name": f"probe-update-{suffix}"})
        r.raise_for_status()
        vid = r.json()["id"]
        creds_path = f"/vaults/{vid}/credentials"

        # Create initial credential (token = "v1").
        r = await call(
            http,
            "POST",
            creds_path,
            {
                "auth": {
                    "mcp_server_url": "https://x.example.com/mcp",
                    "type": "static_bearer",
                    "token": "v1-claimless",
                }
            },
        )
        r.raise_for_status()
        cred = r.json()
        cid = cred["id"]
        cred_path = f"{creds_path}/{cid}"
        print(f"\n>>> Created credential id={cid}")

        # ---- 1. In-place update attempts ----
        # PATCH whole credential
        await call(
            http,
            "PATCH",
            cred_path,
            {
                "auth": {
                    "mcp_server_url": "https://x.example.com/mcp",
                    "type": "static_bearer",
                    "token": "v2-scoped",
                }
            },
        )
        # PATCH auth only
        await call(
            http,
            "PATCH",
            cred_path,
            {"auth": {"token": "v2-scoped"}},
        )
        # PUT whole credential
        await call(
            http,
            "PUT",
            cred_path,
            {
                "auth": {
                    "mcp_server_url": "https://x.example.com/mcp",
                    "type": "static_bearer",
                    "token": "v2-scoped",
                }
            },
        )

        # ---- 2. Add a second credential alongside ----
        r2 = await call(
            http,
            "POST",
            creds_path,
            {
                "auth": {
                    "mcp_server_url": "https://x.example.com/mcp",
                    "type": "static_bearer",
                    "token": "v2-scoped",
                }
            },
        )
        second_cid = r2.json().get("id") if r2.status_code < 300 else None
        print(f"\n>>> Second credential id={second_cid} (status {r2.status_code})")

        # List to observe ordering / selection semantics.
        await call(http, "GET", creds_path)

        # ---- 3. Delete then recreate ----
        await call(http, "DELETE", cred_path)
        # Re-list to confirm deletion.
        await call(http, "GET", creds_path)
        # Recreate.
        r3 = await call(
            http,
            "POST",
            creds_path,
            {
                "auth": {
                    "mcp_server_url": "https://x.example.com/mcp",
                    "type": "static_bearer",
                    "token": "v3-after-delete",
                }
            },
        )
        print(f"\n>>> Recreate after delete: status {r3.status_code}")

        # Final list.
        await call(http, "GET", creds_path)

        # ---- Cleanup ----
        await call(http, "DELETE", f"/vaults/{vid}")
        print("\n=== Probe complete ===")
        print("Interpret results:")
        print("  - PATCH/PUT 2xx → in-place update viable (option a)")
        print("  - PATCH/PUT 4xx + DELETE/POST works → delete+recreate (option b)")
        print("  - POST second credential 2xx + GET shows both → alongside (option c)")


if __name__ == "__main__":
    asyncio.run(main())
