"""R6: confirm `ensure_mcp_vault` picks the OLDEST vault when duplicates exist.

MA enforces no uniqueness on `display_name`, so two vaults with identical
`daimon-mcp:<account_uuid>` can coexist (probe
`mcp_vault_concurrent_create.py` already characterized that). What R6
verifies is the daimon-side guarantee: when the warm path lists matching
vaults, `min(matching, key=created_at)` is the canonical choice. Without
this property the resolver would oscillate between dup vaults and
credentials would be created against whichever one happened to win the
list iteration.

Read-mostly: creates two throwaway vaults under a synthetic account UUID
(NOT in the accounts table), runs `ensure_mcp_vault`, asserts the older
id is returned, then archives both.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid

from anthropic import AsyncAnthropic
from daimon.core.mcp_vault import ensure_mcp_vault


async def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ["DAIMON_ANTHROPIC__API_KEY"]
    client = AsyncAnthropic(api_key=api_key)

    fake_account = uuid.uuid4()
    display = f"daimon-mcp:{fake_account}"
    print(f"== R6: dup-vault resolver picks oldest ==")
    print(f"   synthetic account_id: {fake_account}")
    print(f"   display_name: {display}")

    # Create two vaults with the same display_name, 1.2s apart so created_at differs.
    print("\n[1/4] creating older vault...")
    older = await client.beta.vaults.create(display_name=display)
    print(f"   older.id = {older.id}  created_at = {older.created_at}")
    await asyncio.sleep(1.2)
    print("[2/4] creating newer vault...")
    newer = await client.beta.vaults.create(display_name=display)
    print(f"   newer.id = {newer.id}  created_at = {newer.created_at}")
    assert newer.created_at > older.created_at, "newer must have later created_at"

    try:
        print("\n[3/4] calling ensure_mcp_vault — must pick OLDER...")
        resolved = await ensure_mcp_vault(
            client,
            account_id=fake_account,
            jwt_secret=b"r" * 32,
            public_url="https://r6-probe.example/mcp",
            now=dt.datetime.now(dt.UTC),
            session_context=None,
        )
        print(f"   resolved = {resolved}")
        assert resolved == older.id, (
            f"R6 FAIL: ensure_mcp_vault must pick older ({older.id}) — got {resolved}"
        )
        print(f"\nR6 PASS — resolver picked the older vault deterministically.")
    finally:
        print("\n[4/4] cleanup: archiving both vaults...")
        for vid in (older.id, newer.id):
            try:
                await client.beta.vaults.archive(vid)
                print(f"   archived {vid}")
            except Exception as e:
                print(f"   archive {vid} failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
