"""Characterize MA behavior under concurrent vault creation with identical display_name.

Questions:
  - Do N simultaneous POSTs with the same display_name all succeed?
  - Does MA return 409/422 or some other uniqueness error?
  - Any behavioral difference between concurrent and sequential same-name creates?

Run:
    uv run python scripts/probes/managed_agents/mcp_vault_concurrent_create.py
"""

from __future__ import annotations

import asyncio
import os
import uuid

import httpx
from dotenv import load_dotenv

API_BASE = "https://api.anthropic.com/v1"
BETA = "managed-agents-2026-04-01"

CONCURRENT_N = 5
SEQUENTIAL_N = 3


def hdrs() -> dict[str, str]:
    return {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "anthropic-beta": BETA,
        "content-type": "application/json",
    }


async def create_vault(
    http: httpx.AsyncClient, display_name: str, label: str
) -> tuple[str, int, dict | str, str | None]:
    r = await http.post(
        f"{API_BASE}/vaults",
        headers=hdrs(),
        json={"display_name": display_name},
    )
    try:
        data: dict | str = r.json()
    except Exception:
        data = r.text
    vault_id = data.get("id") if isinstance(data, dict) else None
    return label, r.status_code, data, vault_id


async def delete_vault(http: httpx.AsyncClient, vault_id: str) -> None:
    try:
        r = await http.delete(f"{API_BASE}/vaults/{vault_id}", headers=hdrs())
        print(f"  cleanup DELETE /vaults/{vault_id} → {r.status_code}")
    except Exception as e:
        print(f"  cleanup DELETE /vaults/{vault_id} failed: {e}")


async def main() -> None:
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY not set")

    suffix = uuid.uuid4().hex[:8]
    concurrent_name = f"probe-concurrent-{suffix}"
    sequential_name = f"probe-sequential-{suffix}"

    created_ids: list[str] = []

    async with httpx.AsyncClient(timeout=30.0) as http:

        # ── 1. Concurrent creates ──────────────────────────────────────────────
        print(f"\n=== Concurrent: {CONCURRENT_N} simultaneous POSTs, display_name={concurrent_name!r} ===")
        tasks = [
            create_vault(http, concurrent_name, f"concurrent-{i+1}")
            for i in range(CONCURRENT_N)
        ]
        results = await asyncio.gather(*tasks)

        successes = [(lbl, status, data, oid) for lbl, status, data, oid in results if status < 300]
        failures  = [(lbl, status, data, oid) for lbl, status, data, oid in results if status >= 300]

        for lbl, status, data, oid in results:
            body_preview = str(data)[:200]
            print(f"  [{lbl}] {status}  id={oid}  body={body_preview}")
            if oid:
                created_ids.append(oid)

        print(f"\n  Successes: {len(successes)}/{CONCURRENT_N}")
        print(f"  Failures:  {len(failures)}/{CONCURRENT_N}")
        if failures:
            codes = sorted({status for _, status, _, _ in failures})
            print(f"  Failure status codes seen: {codes}")

        concurrent_ids = [oid for _, _, _, oid in successes if oid]
        unique_ids = set(concurrent_ids)
        print(f"  Unique vault IDs returned: {len(unique_ids)}")
        if len(concurrent_ids) > 1:
            print(f"  → MA created {len(concurrent_ids)} vaults with identical display_name (no uniqueness enforcement)")
        elif len(concurrent_ids) == 1:
            print("  → Only one vault survived — MA may enforce uniqueness or one request raced out")
        else:
            print("  → No vaults created — all concurrent requests failed")

        # ── 2. Sequential creates (control) ───────────────────────────────────
        print(f"\n=== Sequential: {SEQUENTIAL_N} serial POSTs, display_name={sequential_name!r} ===")
        seq_successes = 0
        seq_failures = 0
        for i in range(SEQUENTIAL_N):
            lbl, status, data, oid = await create_vault(http, sequential_name, f"sequential-{i+1}")
            body_preview = str(data)[:200]
            print(f"  [{lbl}] {status}  id={oid}  body={body_preview}")
            if status < 300:
                seq_successes += 1
                if oid:
                    created_ids.append(oid)
            else:
                seq_failures += 1

        print(f"\n  Successes: {seq_successes}/{SEQUENTIAL_N}")
        print(f"  Failures:  {seq_failures}/{SEQUENTIAL_N}")

        # ── 3. Summary ────────────────────────────────────────────────────────
        print("\n=== Summary ===")
        if len(successes) == CONCURRENT_N:
            print("  Concurrent: MA allows all concurrent same-display_name creates → duplicates coexist")
        elif len(successes) == 0:
            print("  Concurrent: MA rejected ALL requests — possible uniqueness enforcement or rate-limit")
        else:
            print(f"  Concurrent: partial — {len(successes)} succeeded, {len(failures)} failed")

        if seq_successes == SEQUENTIAL_N:
            print("  Sequential: MA allows all sequential same-display_name creates → duplicates coexist")
        elif seq_successes == 0:
            print("  Sequential: MA rejected ALL sequential requests")
        else:
            print(f"  Sequential: partial — {seq_successes} succeeded, {seq_failures} failed")

        same = (len(successes) == CONCURRENT_N and seq_successes == SEQUENTIAL_N) or \
               (len(successes) == 0 and seq_successes == 0)
        print(f"  Concurrent vs sequential behavior: {'SAME' if same else 'DIFFERENT'}")

        # ── 4. Cleanup ────────────────────────────────────────────────────────
        print(f"\n=== Cleanup ({len(created_ids)} vaults) ===")
        await asyncio.gather(*[delete_vault(http, vid) for vid in created_ids])

    print("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
