"""Find and archive orphan daimon-mcp vaults.

Handles two display-name formats:
  - Legacy per-account:  ``daimon-mcp:<account_uuid>``
  - Per-agent (current): ``daimon-mcp:<account_uuid>:<agent_uuid>``

In both cases the *account id* is the first ':'-delimited segment after
the ``daimon-mcp:`` prefix. Orphan classification is at account granularity
only: a per-agent vault whose account is live is kept even if that specific
agent has been deleted (forward-only, no per-agent reapage — see W1 in the
context doc). A per-account vault whose account is gone is an orphan.

``ensure_mcp_vault`` (now ``ensure_agent_mcp_vault``) creates one vault per
daimon agent, named ``daimon-mcp:<account_uuid>:<agent_uuid>``. When an
account row is removed from the DB (or was never persisted), its vaults
linger on MA forever — nothing ever attaches a session to them, so the
warm-path rebind never fires and stale credentials never self-heal. This
janitor archives those orphan vaults.

Pure logic lives in ``partition_orphan_vault_ids``; the shell wires the
SDK list + DB account lookup + archive calls together. CLI integration
is a separate concern (a future ``daimon mcp janitor`` command).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsVault
from daimon.core.stores.accounts import load_live_account_ids
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

_DISPLAY_PREFIX = "daimon-mcp:"


@dataclass(frozen=True)
class JanitorReport:
    """One pass of the janitor.

    `orphan_vault_ids`: the canonical answer — vaults whose `account_id` is
    not in the accounts table at the moment of scan.
    `archived_vault_ids`: subset actually archived (empty in dry_run).
    `unparseable_vault_ids`: daimon-mcp:* vaults whose first suffix segment
    is not a UUID — left alone; logged for an operator to inspect.
    Handles both ``daimon-mcp:<account>`` and ``daimon-mcp:<account>:<agent>``
    formats; the account id is always the first ':'-delimited segment.
    """

    orphan_vault_ids: list[str]
    archived_vault_ids: list[str]
    unparseable_vault_ids: list[str]


def partition_orphan_vault_ids(
    vaults: list[BetaManagedAgentsVault],
    *,
    live_account_ids: set[uuid.UUID],
) -> tuple[list[str], list[str]]:
    """Pure partition: (orphans, unparseable) by display_name + live set.

    Handles two display-name formats:
      - Legacy per-account:  ``daimon-mcp:<account_uuid>``
      - Per-agent (current): ``daimon-mcp:<account_uuid>:<agent_uuid>``

    In both cases the account id is the **first** ':'-delimited segment of
    the suffix. Orphan classification is at account granularity only (W1):
    a per-agent vault whose account is live is kept even if the specific
    agent was deleted — forward-only, no per-agent reapage.

    Vaults whose ``display_name`` doesn't start with ``daimon-mcp:`` are
    irrelevant (not ours) and filtered out entirely. Of the daimon-mcp:*
    ones:
      - First suffix segment is a UUID AND uuid is in live_account_ids → not returned.
      - First suffix segment is a UUID AND uuid is NOT in live_account_ids → orphan.
      - First suffix segment isn't a UUID → unparseable.
    """
    orphans: list[str] = []
    unparseable: list[str] = []
    for v in vaults:
        if not v.display_name.startswith(_DISPLAY_PREFIX):
            continue
        suffix = v.display_name[len(_DISPLAY_PREFIX) :]
        account_segment = suffix.split(":", 1)[0]
        try:
            account_id = uuid.UUID(account_segment)
        except ValueError:
            unparseable.append(v.id)
            continue
        if account_id not in live_account_ids:
            orphans.append(v.id)
    return orphans, unparseable


async def archive_orphan_mcp_vaults(
    client: AsyncAnthropic,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    dry_run: bool = True,
) -> JanitorReport:
    """List daimon-mcp:* vaults, cross-reference with accounts, archive orphans.

    Safe by default (`dry_run=True`): returns the orphan list without
    touching MA. Pass `dry_run=False` to actually archive.

    Read-after-write semantics: the live_account_ids set is sampled once at
    the start of the run. A race where an account is created between scan
    and archive would (worst case) archive a vault that just-now had a real
    owner — but `ensure_mcp_vault` will recreate it on first session and
    nothing is lost beyond a few credentials that the warm-path rebind
    rebuilds.
    """
    async with session_factory() as session, session.begin():
        live_account_ids = await load_live_account_ids(session)

    vaults = [v async for v in client.beta.vaults.list()]
    orphan_ids, unparseable_ids = partition_orphan_vault_ids(
        vaults, live_account_ids=live_account_ids
    )

    if unparseable_ids:
        _log.warning(
            "mcp_vault_janitor.unparseable_display_names",
            count=len(unparseable_ids),
            vault_ids=unparseable_ids,
        )

    archived_ids: list[str] = []
    if not dry_run:
        for vault_id in orphan_ids:
            _log.info("mcp_vault_janitor.archive", vault_id=vault_id)
            await client.beta.vaults.archive(vault_id)
            archived_ids.append(vault_id)

    return JanitorReport(
        orphan_vault_ids=orphan_ids,
        archived_vault_ids=archived_ids,
        unparseable_vault_ids=unparseable_ids,
    )
