"""Probe Q1: does MA Vault accept a static_bearer credential carrying a GitHub
PAT with a placeholder mcp_server_url?

The installed `anthropic` SDK exposes only `static_bearer` (requires
`mcp_server_url`) and `mcp_oauth` for vault credential auth. Phase 23 needs to
store a GitHub PAT in MA Vault for `set_repo_binding`. This probe checks
whether `static_bearer` with `mcp_server_url="https://github.com"` is accepted
end-to-end (create + read back + delete), so Plan 03 can lock the upload shape.

Run:
    uv run python scripts/probes/managed_agents/static_bearer_pat.py

Output: a single `RESULT: ok ...` or `RESULT: rejected ...` line on stdout.
The synthetic token value is never logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid

from anthropic import APIError, AsyncAnthropic
from anthropic.types.beta.vaults import BetaManagedAgentsStaticBearerCreateParams

PROBE_VAULT_DISPLAY_NAME = "daimon-probe-q1"
PROBE_PLACEHOLDER_URL = "https://github.com"
# Synthetic, clearly non-PAT placeholder. Never use a real PAT in a probe.
PROBE_SYNTHETIC_TOKEN = "ghp_PROBE_PLACEHOLDER_NOT_A_REAL_PAT_0000"


async def _find_or_create_probe_vault(
    client: AsyncAnthropic,
) -> tuple[str, bool]:
    """Return (vault_id, created_by_us) for the probe vault."""
    matching = [
        v async for v in client.beta.vaults.list() if v.display_name == PROBE_VAULT_DISPLAY_NAME
    ]
    if matching:
        return min(matching, key=lambda v: v.created_at).id, False
    vault = await client.beta.vaults.create(display_name=PROBE_VAULT_DISPLAY_NAME)
    return vault.id, True


async def _run_probe() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("RESULT: skipped reason=ANTHROPIC_API_KEY not set")
        return 0

    client = AsyncAnthropic(api_key=api_key)
    vault_id, created_vault = await _find_or_create_probe_vault(client)

    auth_params: BetaManagedAgentsStaticBearerCreateParams = {
        "type": "static_bearer",
        "mcp_server_url": PROBE_PLACEHOLDER_URL,
        "token": PROBE_SYNTHETIC_TOKEN,
    }

    cred_id: str | None = None
    try:
        cred = await client.beta.vaults.credentials.create(
            vault_id=vault_id,
            auth=auth_params,
            metadata={"probe": "q1", "run_id": uuid.uuid4().hex[:8]},
        )
        cred_id = cred.id
        cred_auth_type = cred.auth.type
        cred_url = cred.auth.mcp_server_url
        logging.info(
            "probe outcome=success credential_type=%s url=%s",
            cred_auth_type,
            cred_url,
        )
        print(
            f"RESULT: ok credential_id={cred_id} auth_type={cred_auth_type} "
            f"mcp_server_url={cred_url}"
        )
        return 0
    except APIError as e:
        # Print rejection result, then re-raise so the operator sees the full
        # traceback for diagnosis (per probe convention: never swallow).
        print(
            f"RESULT: rejected error_type={type(e).__name__} "
            f"status={getattr(e, 'status_code', 'n/a')} message={e!s}"
        )
        raise
    finally:
        if cred_id is not None:
            try:
                await client.beta.vaults.credentials.delete(cred_id, vault_id=vault_id)
            except APIError:
                logging.warning("probe cleanup: failed to delete credential")
        if created_vault:
            try:
                await client.beta.vaults.delete(vault_id)
            except APIError:
                logging.warning("probe cleanup: failed to delete vault")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(asyncio.run(_run_probe()))


if __name__ == "__main__":
    main()
