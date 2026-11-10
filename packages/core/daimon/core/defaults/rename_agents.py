"""Rename a seeded agent in place when the defaults tree renames it.

While the tree ships a new name from `LEGACY_AGENT_RENAMES` and not the old
one, apply moves the tenant's managed agent (its id, sessions, threads and
bindings intact), its reader variant, its self-hosted record and every live
name reference to the new name. It runs before the agent pass and sweep, so
the old agent is never archived and no second one is created.

Order: the database references commit first, then the provider renames. A
failure leaves the managed agent under its old name, so the next apply finds
it again and redoes both; every step is idempotent.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Final

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.defaults.ma_index import find_agents_by_daimon_tag
from daimon.core.defaults.metadata import (
    LEGACY_AGENT_RENAMES,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_READER_SOURCE,
)
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.errors import DefaultsError
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import update_agent
from daimon.core.stores.agent_rename import rename_agent_references, rename_selfhosted_agent
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

_READER_SUFFIX: Final = "-reader"


class AgentRenameBlocked(DefaultsError):
    """The tenant already has its own agent under the new seeded name."""

    def __init__(self, *, old: str, new: str) -> None:
        super().__init__(
            f"This workspace already has its own agent named {new!r}, so the built-in "
            f"{old!r} cannot take that name. Rename or archive that agent, then run "
            "`daimon defaults apply` again."
        )


class AgentRenameAmbiguous(DefaultsError):
    """Two of the tenant's self-hosted records differ from the old name only in case."""

    def __init__(self, *, old: str) -> None:
        super().__init__(
            f"This workspace has more than one self-hosted agent named {old!r} in "
            "different cases. Archive the extra ones, then run `daimon defaults apply` again."
        )


def _only_unmanaged(agents: Collection[BetaManagedAgentsAgent]) -> bool:
    """Whether only a workspace's own agents hold this name, never the seeded one."""
    return bool(agents) and not any(_managed(agent) for agent in agents)


def pending_renames(present_names: Collection[str]) -> list[tuple[str, str]]:
    """The renames this tree asks for: the new name ships and the old one does not."""
    return [
        (old, new)
        for old, new in LEGACY_AGENT_RENAMES.items()
        if new in present_names and old not in present_names
    ]


def _managed(agent: BetaManagedAgentsAgent) -> bool:
    return agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"


def _reader_sources(agent: BetaManagedAgentsAgent) -> list[str]:
    return agent.metadata.get(MA_METADATA_KEY_READER_SOURCE, "").split("\n")


async def _legacy_readers(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID, old: str, new: str, old_agent_live: bool
) -> list[BetaManagedAgentsAgent]:
    """The old agent's reader variants, or none when they cannot move.

    A live agent named `old` that is not the seeded one owns those readers; a
    live `new` reader means the new name already has one. A stray reader is
    harmless, so either case is skipped rather than failed.
    """
    readers = [
        reader
        for reader in await find_agents_by_daimon_tag(
            client, tenant_id=tenant_id, name=f"{old}{_READER_SUFFIX}"
        )
        if old in _reader_sources(reader)
    ]
    if not readers or old_agent_live:
        return []
    if await find_agents_by_daimon_tag(client, tenant_id=tenant_id, name=f"{new}{_READER_SUFFIX}"):
        _log.warning("defaults.rename_reader_taken", tenant_id=str(tenant_id), name=new)
        return []
    return readers


async def _rename_on_provider(
    client: AsyncAnthropic,
    agent: BetaManagedAgentsAgent,
    *,
    tenant_id: uuid.UUID,
    old: str,
    new: str,
) -> None:
    scope = resource_scope(tenant_id=str(tenant_id))

    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        metadata = dict(fresh.metadata)
        name = metadata.get(MA_METADATA_KEY_NAME, "")
        renamed = new + name.removeprefix(old) if name.startswith(old) else new
        metadata[MA_METADATA_KEY_NAME] = renamed
        if MA_METADATA_KEY_READER_SOURCE in metadata:
            metadata[MA_METADATA_KEY_READER_SOURCE] = "\n".join(
                dict.fromkeys(new if s == old else s for s in _reader_sources(fresh))
            )
        return await update_agent(
            client,
            fresh.id,
            version=fresh.version,
            payload={"name": renamed, "metadata": metadata},
            scope=scope,
        )

    await update_agent_with_version_retry(client, agent.id, _apply, scope=scope)


async def rename_seeded_agent(
    client: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    old: str,
    new: str,
    lock_key: str,
    dry_run: bool,
) -> ResourceOutcome | None:
    """Rename the tenant's seeded `old` to `new`; None when there is nothing to do.

    Raises `AgentRenameBlocked` when the tenant's own unmanaged agent or
    self-hosted record holds `new`, with or without a seeded `old`, and
    `AgentRenameAmbiguous` when self-hosted records differ from `old` only in
    case: nothing is renamed and the caller skips the agent pass, so that agent is never adopted.
    """
    olds = await find_agents_by_daimon_tag(client, tenant_id=tenant_id, name=old)
    legacy = [agent for agent in olds if _managed(agent)]
    if _only_unmanaged(await find_agents_by_daimon_tag(client, tenant_id=tenant_id, name=new)):
        raise AgentRenameBlocked(old=old, new=new)
    readers = await _legacy_readers(
        client, tenant_id=tenant_id, old=old, new=new, old_agent_live=len(olds) > len(legacy)
    )
    if dry_run:
        if not legacy and not readers:
            return None
        return ResourceOutcome(kind="agent", name=new, action=Action.RENAMED)

    async with session_factory() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": lock_key}
        )
        selfhosted = await rename_selfhosted_agent(session, tenant_id=tenant_id, old=old, new=new)
        if selfhosted == "taken":
            raise AgentRenameBlocked(old=old, new=new)
        if selfhosted == "ambiguous":
            raise AgentRenameAmbiguous(old=old)
        if legacy or selfhosted == "renamed":
            await rename_agent_references(session, tenant_id=tenant_id, old=old, new=new)
    for agent in (*legacy, *readers):
        await _rename_on_provider(client, agent, tenant_id=tenant_id, old=old, new=new)
    if not legacy and not readers and selfhosted != "renamed":
        return None
    _log.info("defaults.agent_renamed", tenant_id=str(tenant_id), old=old, new=new)
    return ResourceOutcome(
        kind="agent",
        name=new,
        action=Action.RENAMED,
        anthropic_id=legacy[0].id if legacy else None,
    )
