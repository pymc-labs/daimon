"""Sweep stale ``is_admin`` claims from existing daimon-mcp vault credentials.

**Operator invocation:** ``daimon mcp sweep-credentials`` (dry-run by default;
pass ``--apply`` to mutate).

**Defense-in-depth / dormant-cred cleanup — NOT load-bearing for the #162
escalation.**

The #162 escalation (intra-tenant RBAC bypass via confused-deputy threads) is
closed at the 88-03 gate: a baked ``is_admin`` claim on a *non-internal* Discord
vault token never elevates a non-admin caller at the MCP admin gate, regardless
of whether or when this sweep runs. The 88-03 gate alone is sufficient.

This module performs a *one-time backfill* to align the existing credential
fleet with the post-88-03 invariant: ``mint_jwt`` called from
``ensure_agent_mcp_vault`` now omits ``is_admin`` from long-lived credentials,
but credentials minted before the 88-03 fix may still carry a frozen
``is_admin=True`` claim with no expiry. While these stale claims are already
inert (the gate checks ``is_admin AND internal`` together; Discord vault tokens
never carry ``internal``), sweeping them removes the confusion from operator
introspection and tightens the security posture.

**Scope:** matches ONLY ``static_bearer`` credentials whose
``auth.mcp_server_url`` equals the operator's current ``public_url``. Never
touches GitHub Copilot credentials (``GITHUB_COPILOT_MCP_URL``) or any other
user-added external MCP credentials.

**Shape (mirrors mcp_vault_janitor.py):**
- Pure ``partition_daimon_mcp_vault_ids(vaults) -> list[str]``: selects vault
  ids whose ``display_name`` starts with ``daimon-mcp:``.
- Frozen ``SweepReport`` dataclass capturing planned/swept pairs, recreated
  credential ids (empty in dry-run), and skipped/unparseable vault ids.
- Shell ``sweep_stale_admin_credentials(client, *, jwt_secret, public_url,
  now, dry_run=True) -> SweepReport``: list vaults → partition → for each
  daimon-mcp vault parse account UUID → list credentials → identify the
  ``static_bearer`` at ``public_url`` → in non-dry-run delete then recreate
  with ``mint_jwt(account_id=..., secret=jwt_secret, now=now)`` (no
  ``is_admin``).

No global state; ``client`` / ``jwt_secret`` / ``now`` are injected per
``guideline:architecture``. Let ``anthropic.APIError`` propagate.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsVault
from daimon.core.mcp_auth import mint_jwt
from daimon.core.mcp_vault import GITHUB_COPILOT_MCP_URL

_log = structlog.get_logger(__name__)

_DISPLAY_PREFIX = "daimon-mcp:"


@dataclass(frozen=True)
class SweepReport:
    """One pass of the credential sweep.

    ``swept_pairs``: ``(vault_id, old_cred_id)`` tuples targeted by the sweep
    — populated in both dry-run (planned) and apply (actually swept) mode.
    ``recreated_cred_ids``: ids of freshly created credentials (empty in
    dry-run).
    ``unparseable_vault_ids``: daimon-mcp:* vaults whose first suffix segment
    is not a UUID — skipped and left alone; logged for operator inspection.
    """

    swept_pairs: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    recreated_cred_ids: list[str] = field(default_factory=list[str])
    unparseable_vault_ids: list[str] = field(default_factory=list[str])


def partition_daimon_mcp_vault_ids(
    vaults: list[BetaManagedAgentsVault],
) -> list[str]:
    """Pure filter: return vault ids whose display_name starts with ``daimon-mcp:``.

    Non-daimon-mcp vaults (Copilot builtins, operator-named vaults, etc.) are
    filtered out entirely — this sweep has no mandate over them.

    Both ``daimon-mcp:<account_uuid>`` (legacy per-account) and
    ``daimon-mcp:<account_uuid>:<agent_uuid>`` (per-agent, current) formats are
    included. Parsing / UUID validation is left to the caller (the sweep shell
    does it to extract the account_id).
    """
    return [v.id for v in vaults if v.display_name.startswith(_DISPLAY_PREFIX)]


async def sweep_stale_admin_credentials(
    client: AsyncAnthropic,
    *,
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
    dry_run: bool = True,
) -> SweepReport:
    """List daimon-mcp:* vaults and delete+recreate stale is_admin credentials.

    Safe by default (``dry_run=True``): returns the planned targets without
    touching MA. Pass ``dry_run=False`` to actually delete+recreate.

    For each daimon-mcp vault:
    1. Parse the account UUID from the display_name first suffix segment after
       ``daimon-mcp:`` (e.g. ``daimon-mcp:<account>:<agent>`` → ``<account>``).
       Vaults with non-UUID first segments are skipped and recorded as
       unparseable.
    2. List credentials for the vault.
    3. Find the ``static_bearer`` credential whose
       ``auth.mcp_server_url == public_url``. Skip non-static_bearer and
       non-matching-URL credentials — this is the Copilot / external-cred
       protection.
    4. In non-dry-run: DELETE the matched credential, then POST a fresh
       ``static_bearer`` at ``public_url`` minted via ``mint_jwt(account_id=...,
       secret=jwt_secret, now=now)`` with NO ``is_admin``. In dry-run: record
       the planned (vault_id, cred_id) target without writing anything.

    MA blocks PATCH (405) and duplicate POST (409) on credentials, so
    delete+recreate is the only way to update a credential.

    ``anthropic.APIError`` propagates — callers at the CLI boundary decide how
    to surface failures.
    """
    all_vaults = [v async for v in client.beta.vaults.list()]
    daimon_mcp_ids = partition_daimon_mcp_vault_ids(all_vaults)

    # Build a lookup from vault_id → BetaManagedAgentsVault for display_name access.
    vault_by_id: dict[str, BetaManagedAgentsVault] = {v.id: v for v in all_vaults}

    swept_pairs: list[tuple[str, str]] = []
    recreated_cred_ids: list[str] = []
    unparseable_vault_ids: list[str] = []

    for vault_id in daimon_mcp_ids:
        vault = vault_by_id[vault_id]
        suffix = vault.display_name[len(_DISPLAY_PREFIX) :]
        account_segment = suffix.split(":", 1)[0]
        try:
            account_id = uuid.UUID(account_segment)
        except ValueError:
            _log.warning(
                "mcp_credential_sweep.unparseable_display_name",
                vault_id=vault_id,
                display_name=vault.display_name,
            )
            unparseable_vault_ids.append(vault_id)
            continue

        # Find the static_bearer credential at public_url (skip all others).
        target_cred_id: str | None = None
        async for cred in client.beta.vaults.credentials.list(vault_id=vault_id):
            if cred.auth.type != "static_bearer":
                continue
            if cred.auth.mcp_server_url == GITHUB_COPILOT_MCP_URL:
                continue  # Never touch Copilot credentials.
            if cred.auth.mcp_server_url == public_url:
                target_cred_id = cred.id
                break  # First match is sufficient; URLs are effectively unique.

        if target_cred_id is None:
            # No static_bearer at public_url — nothing to sweep for this vault.
            continue

        swept_pairs.append((vault_id, target_cred_id))

        if dry_run:
            _log.info(
                "mcp_credential_sweep.planned",
                vault_id=vault_id,
                cred_id=target_cred_id,
                account_id=str(account_id),
            )
            continue

        # Apply: delete the stale credential, create a fresh one without is_admin.
        _log.info(
            "mcp_credential_sweep.delete",
            vault_id=vault_id,
            cred_id=target_cred_id,
            account_id=str(account_id),
        )
        await client.beta.vaults.credentials.delete(
            target_cred_id,
            vault_id=vault_id,
        )

        token = mint_jwt(
            account_id=account_id,
            secret=jwt_secret,
            now=now,
            # No is_admin — this is the invariant: Discord vault creds carry no is_admin.
        )
        new_cred = await client.beta.vaults.credentials.create(
            vault_id=vault_id,
            auth={
                "type": "static_bearer",
                "mcp_server_url": public_url,
                "token": token,
            },
        )
        _log.info(
            "mcp_credential_sweep.recreated",
            vault_id=vault_id,
            new_cred_id=new_cred.id,
            account_id=str(account_id),
        )
        recreated_cred_ids.append(new_cred.id)

    return SweepReport(
        swept_pairs=swept_pairs,
        recreated_cred_ids=recreated_cred_ids,
        unparseable_vault_ids=unparseable_vault_ids,
    )
