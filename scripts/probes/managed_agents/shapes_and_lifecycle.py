"""Probe Managed Agents pagination, session detail, send_message lifecycle, vault.

Covers:
- {data, next_page} pagination cursor shape
- Session object shape (for sessions.list output design)
- send_message into sessions in different states
- list_vault_credentials shape — verify values never returned
- Error shape for common 4xx cases
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
from dotenv import load_dotenv

API_BASE = "https://api.anthropic.com/v1"
BETA = "managed-agents-2026-04-01"


def headers() -> dict[str, str]:
    return {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "anthropic-beta": BETA,
        "content-type": "application/json",
    }


async def get(http: httpx.AsyncClient, path: str, **kwargs) -> tuple[int, dict | str]:
    r = await http.get(f"{API_BASE}{path}", headers=headers(), **kwargs)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


async def post(http: httpx.AsyncClient, path: str, body: dict) -> tuple[int, dict | str]:
    r = await http.post(f"{API_BASE}{path}", headers=headers(), json=body)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


async def main() -> None:
    load_dotenv()
    async with httpx.AsyncClient(timeout=30) as http:
        # --- Pagination ---
        print("== Pagination: GET /agents?limit=2 ==")
        status, body = await get(http, "/agents", params={"limit": 2})
        print(f"  {status}; keys={list(body.keys()) if isinstance(body, dict) else '?'}")
        if isinstance(body, dict):
            nxt = body.get("next_page")
            print(f"  next_page value: {nxt!r}")
            print(f"  data len: {len(body.get('data', []))}")
            # Follow next_page if present
            if nxt:
                # Try as a cursor: ?page_cursor=... or ?cursor=... or prefix path?
                for param in ("page_cursor", "cursor", "next_page", "page"):
                    s2, b2 = await get(http, "/agents", params={"limit": 2, param: nxt})
                    if s2 == 200 and isinstance(b2, dict):
                        # Check if we got different data
                        new_ids = {a["id"] for a in b2.get("data", [])} if b2.get("data") else set()
                        old_ids = (
                            {a["id"] for a in body.get("data", [])} if body.get("data") else set()
                        )
                        if new_ids and new_ids.isdisjoint(old_ids):
                            print(f"  cursor param works: {param!r} → new ids {new_ids}")
                            break
                        else:
                            print(f"  {param!r}: 200 but same data (not a cursor)")
                    else:
                        print(f"  {param!r}: {s2}")

        # --- Session shape ---
        print("\n== Session detail shape ==")
        status, body = await get(http, "/sessions", params={"limit": 1})
        sess = None
        if isinstance(body, dict) and body.get("data"):
            sess = body["data"][0]
            print(f"  top-level keys: {sorted(sess.keys())}")
            # State
            state_keys = [k for k in sess if "state" in k.lower() or "status" in k.lower()]
            print(f"  state-ish keys: {state_keys}")
            if "state" in sess:
                print(f"  state value: {sess['state']!r}")
            if "status" in sess:
                print(f"  status value: {sess['status']!r}")

        # --- send_message lifecycle ---
        # Check state of sess and try send_message into it
        if sess:
            sid = sess["id"]
            # Don't actually send — read events first for shape
            print(f"\n== GET /sessions/{sid}/events ==")
            status, body = await get(http, f"/sessions/{sid}/events", params={"limit": 3})
            print(f"  {status}")
            if isinstance(body, dict):
                print(f"  keys: {list(body.keys())}")
                if body.get("data"):
                    ev_types = [e.get("type") for e in body["data"]]
                    print(f"  event types in first 3: {ev_types}")

        # --- Vault list shape ---
        print("\n== GET /vaults ==")
        status, body = await get(http, "/vaults", params={"limit": 3})
        if isinstance(body, dict) and body.get("data"):
            v = body["data"][0]
            print(f"  vault keys: {sorted(v.keys())}")
            # No 'value' or 'credential' field expected
            leak_keys = [
                k
                for k in v
                if "value" in k.lower()
                or "secret" in k.lower()
                or "credential" in k.lower()
                or "token" in k.lower()
            ]
            print(f"  leak-shaped keys: {leak_keys}")

            # Credentials for first vault
            vid = v["id"]
            print(f"\n== GET /vaults/{vid}/credentials ==")
            status, body = await get(http, f"/vaults/{vid}/credentials")
            if isinstance(body, dict) and body.get("data"):
                c = body["data"][0]
                print(f"  credential keys: {sorted(c.keys())}")
                leak = [
                    k
                    for k in c
                    if "value" in k.lower() or "secret" in k.lower() or "token" in k.lower()
                ]
                print(f"  leak-shaped keys: {leak}")

        # --- Error shape: unknown session ---
        print("\n== Error: GET /sessions/bogus-id ==")
        status, body = await get(http, "/sessions/bogus-id")
        print(f"  {status}: {body if isinstance(body, str) else json.dumps(body)[:300]}")

        # --- Error: unknown agent ---
        print("\n== Error: GET /agents/bogus-id ==")
        status, body = await get(http, "/agents/bogus-id")
        print(f"  {status}: {body if isinstance(body, str) else json.dumps(body)[:300]}")

        # --- Error: unauthorized ---
        print("\n== Error: bad API key ==")
        r = await http.get(f"{API_BASE}/agents", headers={**headers(), "x-api-key": "sk-bogus"})
        print(f"  {r.status_code}: {r.text[:300]}")


if __name__ == "__main__":
    asyncio.run(main())
