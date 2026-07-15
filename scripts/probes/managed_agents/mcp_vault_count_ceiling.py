"""Characterize the MA per-workspace vault-count ceiling.

Question (RESEARCH Q1): Approach B creates O(accounts × agents) vaults plus
inert per-account orphans. Is there a hard per-workspace vault-count cap?
What is it?

This probe:
  1. Records the current vault count (GET /v1/vaults, paginated).
  2. Creates probe vaults in small batches under a synthetic account UUID,
     named ``probe-vaultcap:{PROBE_ACCOUNT}:{n}``.
  3. After each batch, checks for a 4xx response (429/403/400 quota).
  4. Stops at the first quota error OR at SELF_CEILING (whichever comes first).
  5. Deletes every vault it created in a ``finally`` block — guaranteed cleanup.
  6. Reports the number of vaults successfully created and whether a cap was hit.

Self-cleaning guarantee: every vault created by this probe is deleted in the
``finally`` block regardless of errors. The display names are prefixed
``probe-vaultcap:`` for easy identification if a crash leaves survivors.

Run:
    uv run python scripts/probes/managed_agents/mcp_vault_count_ceiling.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

import httpx
from dotenv import load_dotenv

API_BASE = "https://api.anthropic.com/v1"
BETA = "managed-agents-2026-04-01"

# How many vaults to create per batch before checking for a quota error.
BATCH = 25
# Hard self-imposed ceiling: stop after creating this many probe vaults
# regardless of whether MA has returned an error. Prevents unbounded creation.
SELF_CEILING = 150
# Synthetic account UUID used in all probe vault display names.
PROBE_ACCOUNT = uuid.uuid4()


def hdrs() -> dict[str, str]:
    return {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "anthropic-beta": BETA,
        "content-type": "application/json",
    }


async def get_current_vault_count(http: httpx.AsyncClient) -> int:
    """Return the total number of vaults currently in the workspace (paginated)."""
    count = 0
    after: str | None = None
    while True:
        params: dict[str, str] = {}
        if after is not None:
            params["after_id"] = after
        r = await http.get(f"{API_BASE}/vaults", headers=hdrs(), params=params)
        r.raise_for_status()
        body = r.json()
        data: list[dict[str, object]] = body.get("data", [])
        count += len(data)
        if not body.get("has_more", False) or not data:
            break
        last = data[-1]
        after = str(last["id"])
    return count


async def create_vault(http: httpx.AsyncClient, display_name: str) -> tuple[int, str | None]:
    """POST a single vault; return (status_code, vault_id_or_None)."""
    r = await http.post(
        f"{API_BASE}/vaults",
        headers=hdrs(),
        json={"display_name": display_name},
    )
    vault_id: str | None = None
    if r.status_code < 300:
        with contextlib.suppress(Exception):
            vault_id = r.json().get("id")
    return r.status_code, vault_id


async def delete_vault(http: httpx.AsyncClient, vault_id: str) -> None:
    """DELETE a single vault; log result but do not raise."""
    try:
        r = await http.delete(f"{API_BASE}/vaults/{vault_id}", headers=hdrs())
        print(f"  cleanup DELETE /vaults/{vault_id} → {r.status_code}")
    except Exception as exc:
        print(f"  cleanup DELETE /vaults/{vault_id} FAILED: {exc}")


async def main() -> None:
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY not set — run from repo root with .env present")

    created_ids: list[str] = []

    async with httpx.AsyncClient(timeout=30.0) as http:
        try:
            initial_count = await get_current_vault_count(http)
            print(f"Current vault count in workspace: {initial_count}")
            print(
                f"Probe account: {PROBE_ACCOUNT}  |  batch={BATCH}  |  self-ceiling={SELF_CEILING}"
            )

            cap_hit = False
            cap_status: int | None = None
            total_attempted = 0

            batch_num = 0
            while total_attempted < SELF_CEILING:
                batch_num += 1
                remaining = SELF_CEILING - total_attempted
                this_batch = min(BATCH, remaining)
                print(f"\n--- Batch {batch_num}: creating {this_batch} vaults ---")

                for i in range(this_batch):
                    n = total_attempted + i
                    display_name = f"probe-vaultcap:{PROBE_ACCOUNT}:{n}"
                    status, vault_id = await create_vault(http, display_name)
                    total_attempted += 1

                    if vault_id:
                        created_ids.append(vault_id)

                    if status >= 400:
                        r_body = f"status={status}"
                        print(
                            f"  vault #{n}: {status} — quota/cap error detected after"
                            f" {total_attempted} vaults. body={r_body!r}"
                        )
                        cap_hit = True
                        cap_status = status
                        break

                    if i % 5 == 0:
                        print(f"  vault #{n}: ok (id={vault_id})")

                if cap_hit:
                    break

                print(f"  batch {batch_num} done — {total_attempted} vaults created so far")

            print("\n=== Results ===")
            print(f"  Vaults successfully created:  {len(created_ids)}")
            print(f"  Total attempted:              {total_attempted}")
            print(f"  Self-ceiling:                 {SELF_CEILING}")
            if cap_hit:
                print(f"  Cap hit at vault #{total_attempted} — HTTP status {cap_status}")
            else:
                print(
                    f"  No cap observed up to self-ceiling ({SELF_CEILING})."
                    " Actual MA limit is higher (or unlimited)."
                )

        finally:
            if created_ids:
                print(f"\n=== Cleanup: deleting {len(created_ids)} probe vaults ===")
                await asyncio.gather(*[delete_vault(http, vid) for vid in created_ids])
                print("  cleanup complete")
            else:
                print("\n=== Cleanup: no vaults to delete ===")

    print("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
