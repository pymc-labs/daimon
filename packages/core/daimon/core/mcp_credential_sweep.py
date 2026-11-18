"""Sweep stale ``is_admin`` claims from existing daimon-mcp vault credentials.

**Operator invocation:** ``daimon mcp sweep-credentials`` (dry-run by default;
pass ``--apply`` to mutate).

**Defense-in-depth / dormant-cred cleanup — NOT load-bearing for the admin
escalation.**

The admin escalation (intra-tenant RBAC bypass via confused-deputy threads) is
closed at the MCP admin gate: a baked ``is_admin`` claim on a *non-internal* Discord
vault token never elevates a non-admin caller at the MCP admin gate, regardless
of whether or when this sweep runs. The gate alone is sufficient.

This one-time backfill replaces stale credentials with the same registered,
expiring ordinary-chat credential the per-turn vault owner mints. Retired
per-account names without an executing agent are archived and reported;
creating another unbounded account bearer would undo the vault invariant.

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
  with a registered expiring ordinary-chat credential (no ``is_admin``).

No global state; dependencies are injected. Individual vault failures are
reported without blocking other vaults. Failure to list vaults propagates.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

import structlog
from anthropic import APIError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsVault
from daimon.core.channel_backend import backend_refusal_causes
from daimon.core.mcp_vault import GITHUB_COPILOT_MCP_URL, chat_mcp_credential, same_server_url
from daimon.core.mux_backend import platform_scope
from daimon.core.mux_compat import (
    archive_vault,
    create_credential,
    delete_credential,
    list_credentials,
    list_vaults,
)
from daimon.core.session_ports_compat import mounted_live_vault_ids
from mux.contracts.ids import Scope
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

_DISPLAY_PREFIX = "daimon-mcp:"


@dataclass(frozen=True)
class SweepReport:
    """One pass of the credential sweep.

    ``swept_pairs``: ``(vault_id, old_cred_id)`` tuples targeted by the sweep
    — populated in both dry-run (planned) and apply (actually swept) mode.
    ``recreated_cred_ids``: ids of freshly created credentials (empty in
    dry-run).
    ``unparseable_vault_ids``: daimon-mcp:* vaults without valid account/agent
    binding or private namespace — left alone for operator inspection.
    ``archived_vault_ids``: retired per-account namespaces archived (planned
    in dry-run). ``failures``: per-vault error types, with other vaults continued.
    """

    swept_pairs: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    recreated_cred_ids: list[str] = field(default_factory=list[str])
    unparseable_vault_ids: list[str] = field(default_factory=list[str])
    archived_vault_ids: list[str] = field(default_factory=list[str])
    failures: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    in_use_vault_ids: list[str] = field(default_factory=list[str])


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
    session_factory: async_sessionmaker[AsyncSession],
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
    dry_run: bool = True,
) -> SweepReport:
    """List daimon-mcp:* vaults and delete+recreate stale is_admin credentials.

    Safe by default (``dry_run=True``): returns the planned targets without
    touching MA. Pass ``dry_run=False`` to actually delete+recreate.

    For each daimon-mcp vault:
    1. Parse account, executing agent and optional isolated Slack context from
       ``daimon-mcp:<account>:<agent>[:slack-turn:<context>]``. Unsupported or
       malformed names are reported. Retired per-account vaults are archived
       without creating another unbounded account credential.
    2. List credentials for the vault.
    3. Find the ``static_bearer`` credential whose
       ``auth.mcp_server_url == public_url``. Skip non-static_bearer and
       non-matching-URL credentials — this is the Copilot / external-cred
       protection.
    4. In non-dry-run: DELETE the matched credential, then POST a fresh
       registered, expiring ``static_bearer`` at ``public_url`` with no
       ``is_admin``. In dry-run: record
       the planned (vault_id, cred_id) target without writing anything.

    The explicit operator sweep retains its delete+recreate behavior. Normal
    turns use the vault owner's in-place refresh instead.

    Each vault isolates provider, database and identity failures in the report.
    A failure listing the inventory still propagates to the CLI boundary.
    """
    scope = platform_scope("mcp_credential_sweep.sweep_stale_admin_credentials")
    all_vaults = [v async for v in list_vaults(client, scope=scope)]
    daimon_mcp_ids = partition_daimon_mcp_vault_ids(all_vaults)

    # Build a lookup from vault_id → BetaManagedAgentsVault for display_name access.
    vault_by_id: dict[str, BetaManagedAgentsVault] = {v.id: v for v in all_vaults}

    swept_pairs: list[tuple[str, str]] = []
    recreated_cred_ids: list[str] = []
    unparseable_vault_ids: list[str] = []

    archived_vault_ids: list[str] = []
    failures: list[tuple[str, str]] = []
    in_use_vault_ids: list[str] = []

    for vault_id in daimon_mcp_ids:
        vault = vault_by_id[vault_id]
        if vault.archived_at is not None:
            continue
        suffix = vault.display_name[len(_DISPLAY_PREFIX) :]
        parts = suffix.split(":")
        try:
            account_id = uuid.UUID(parts[0])
            if len(parts) == 1:
                try:
                    if vault_id in await mounted_live_vault_ids(client, scope=scope):
                        in_use_vault_ids.append(vault_id)
                        continue
                    if not dry_run:
                        await archive_vault(client, vault_id, scope=scope)
                    archived_vault_ids.append(vault_id)
                except (APIError, SQLAlchemyError, ValueError) as exc:
                    failures.append((vault_id, type(exc).__name__))
                continue
            agent_id = uuid.UUID(parts[1])
            context_id = None
            if len(parts) != 2:
                if len(parts) != 4 or parts[2] != "slack-turn":
                    raise ValueError("unsupported vault namespace")
                context_id = uuid.UUID(parts[3])
        except (ValueError, IndexError):
            _log.warning(
                "mcp_credential_sweep.unparseable_display_name",
                vault_id=vault_id,
                display_name=vault.display_name,
            )
            unparseable_vault_ids.append(vault_id)
            continue

        try:
            await _sweep_vault(
                client,
                scope=scope,
                session_factory=session_factory,
                jwt_secret=jwt_secret,
                public_url=public_url,
                now=now,
                dry_run=dry_run,
                vault_id=vault_id,
                account_id=account_id,
                agent_id=agent_id,
                context_id=context_id,
                swept_pairs=swept_pairs,
                recreated_cred_ids=recreated_cred_ids,
            )
        except (APIError, SQLAlchemyError, ValueError) as exc:
            # Never include credential payloads, JWTs or arbitrary provider error text.
            failures.append((vault_id, type(exc).__name__))
            _log.warning(
                "mcp_credential_sweep.failed",
                vault_id=vault_id,
                error_type=type(exc).__name__,
                causes=backend_refusal_causes(exc),
            )

    return SweepReport(
        swept_pairs=swept_pairs,
        recreated_cred_ids=recreated_cred_ids,
        unparseable_vault_ids=unparseable_vault_ids,
        archived_vault_ids=archived_vault_ids,
        failures=failures,
        in_use_vault_ids=in_use_vault_ids,
    )


async def _sweep_vault(
    client: AsyncAnthropic,
    *,
    scope: Scope,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
    dry_run: bool,
    vault_id: str,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    context_id: uuid.UUID | None,
    swept_pairs: list[tuple[str, str]],
    recreated_cred_ids: list[str],
) -> None:
    # Find the static_bearer credential at public_url (skip all others).
    target_cred_id: str | None = None
    async for cred in list_credentials(client, vault_id, scope=scope):
        if cred.auth.type != "static_bearer":
            continue
        if same_server_url(cred.auth.mcp_server_url, GITHUB_COPILOT_MCP_URL):
            continue  # Never touch Copilot credentials.
        if same_server_url(cred.auth.mcp_server_url, public_url):
            target_cred_id = cred.id
            break  # First match is sufficient; URLs are effectively unique.

    if target_cred_id is None:
        # No static_bearer at public_url — nothing to sweep for this vault.
        return

    if dry_run:
        swept_pairs.append((vault_id, target_cred_id))
        _log.info(
            "mcp_credential_sweep.planned",
            vault_id=vault_id,
            cred_id=target_cred_id,
            account_id=str(account_id),
        )
        return

    # Commit the registry row before publishing the replacement bearer.
    async with session_factory() as session, session.begin():
        payload = await chat_mcp_credential(
            session,
            account_id=account_id,
            agent_id=agent_id,
            jwt_secret=jwt_secret,
            public_url=public_url,
            now=now,
            slack_turn_context_id=context_id,
        )

    _log.info(
        "mcp_credential_sweep.delete",
        vault_id=vault_id,
        cred_id=target_cred_id,
        account_id=str(account_id),
    )
    await delete_credential(client, vault_id, target_cred_id, scope=scope)

    new_cred = await create_credential(client, vault_id, payload, scope=scope)
    _log.info(
        "mcp_credential_sweep.recreated",
        vault_id=vault_id,
        new_cred_id=new_cred.id,
        account_id=str(account_id),
    )
    swept_pairs.append((vault_id, target_cred_id))
    recreated_cred_ids.append(new_cred.id)
