"""Rename a seeded agent in place when the defaults tree renames it.

While the tree ships `default` and not `daimon`, apply moves the tenant's
managed agent (its id, sessions, threads and bindings intact), its reader
variant, its self-hosted record and every live name reference to the new name.
It runs before the agent pass and sweep, so the old agent is never archived and
no second one is created.

Order: the database references commit first, then the provider renames, then
the access rules. A failure leaves the managed agent under its old name, so the
next apply finds it again and redoes them; every step is idempotent.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import (
    BUILTIN_AGENT_NAME,
    LEGACY_BUILTIN_AGENT_NAME,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_READER_OF,
    MA_METADATA_KEY_READER_SOURCE,
)
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.errors import DefaultsError
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import update_agent
from daimon.core.stores.agent_avatars import normalize_agent_name
from daimon.core.stores.agent_rename import (
    SelfHostedState,
    inspect_selfhosted_rename,
    rename_agent_references,
    rename_policy_keys,
    rename_selfhosted_agent,
)
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
    old, new = LEGACY_BUILTIN_AGENT_NAME, BUILTIN_AGENT_NAME
    return [(old, new)] if new in present_names and old not in present_names else []


def _managed(agent: BetaManagedAgentsAgent) -> bool:
    return agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"


def _reader_sources(agent: BetaManagedAgentsAgent) -> list[str]:
    return agent.metadata.get(MA_METADATA_KEY_READER_SOURCE, "").split("\n")


def _named(agents: Collection[BetaManagedAgentsAgent], name: str) -> list[BetaManagedAgentsAgent]:
    """`agents` whose name matches `name` in any case, as agent names compare."""
    wanted = normalize_agent_name(name)
    return [
        agent
        for agent in agents
        if normalize_agent_name(agent.metadata.get(MA_METADATA_KEY_NAME, "")) == wanted
    ]


def _legacy_readers(
    agents: Collection[BetaManagedAgentsAgent], *, old: str, new: str, old_agent_live: bool
) -> list[BetaManagedAgentsAgent]:
    """The seeded old agent's reader variants, which move with it.

    A live agent named `old` that is not the seeded one owns those readers, so
    they stay. A live `new` reader blocks the rename: access rules follow the
    new name, so a reader left behind would lose them. The exception is a
    `new` reader of the renamed seeded agent itself (a turn made it after the
    agent moved but before its old reader did): it already serves that agent,
    so the stale old reader is left as it is, which is how readers are
    treated when nothing else uses them.
    """
    readers = [
        reader
        for reader in _named(agents, f"{old}{_READER_SUFFIX}")
        if old in _reader_sources(reader)
    ]
    if not readers or old_agent_live:
        return []
    taken = _named(agents, f"{new}{_READER_SUFFIX}")
    if taken and _serves_renamed_seeded(agents, taken, new=new):
        return []
    if taken:
        raise AgentRenameBlocked(old=f"{old}{_READER_SUFFIX}", new=f"{new}{_READER_SUFFIX}")
    return readers


def _serves_renamed_seeded(
    agents: Collection[BetaManagedAgentsAgent],
    readers: Collection[BetaManagedAgentsAgent],
    *,
    new: str,
) -> bool:
    """Whether every reader is a reader variant of the seeded agent now named `new`."""
    return any(_managed(agent) for agent in _named(agents, new)) and all(
        MA_METADATA_KEY_READER_OF in reader.metadata
        and normalize_agent_name(new) in map(normalize_agent_name, _reader_sources(reader))
        for reader in readers
    )


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
        # Compare as `_named` does, which picked this agent; a reader's suffix is plain ASCII.
        folded, folded_old = normalize_agent_name(name), normalize_agent_name(old)
        if not folded.startswith(folded_old):
            return fresh  # a concurrent apply renamed it first
        renamed = new + folded.removeprefix(folded_old)
        metadata[MA_METADATA_KEY_NAME] = renamed
        if MA_METADATA_KEY_READER_SOURCE in metadata:
            metadata[MA_METADATA_KEY_READER_SOURCE] = "\n".join(
                dict.fromkeys(
                    new if normalize_agent_name(s) == normalize_agent_name(old) else s
                    for s in _reader_sources(fresh)
                )
            )
        return await update_agent(
            client,
            fresh.id,
            version=fresh.version,
            payload={"name": renamed, "metadata": metadata},
            scope=scope,
        )

    await update_agent_with_version_retry(client, agent.id, _apply, scope=scope)


@dataclass(frozen=True)
class SeededRename:
    """A tenant's due rename of its seeded `old` to `new`, planned from one listing."""

    old: str
    new: str
    # The provider agents that move: the seeded agent, then its readers.
    moves: tuple[BetaManagedAgentsAgent, ...]
    outcome: ResourceOutcome


async def plan_seeded_rename(
    agents: Collection[BetaManagedAgentsAgent],
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    old: str,
    new: str,
) -> SeededRename | None:
    """The rename the tenant's live `agents` and records call for; None when none is due.

    Raises `AgentRenameBlocked` when the tenant's own unmanaged agent or
    self-hosted record holds `new` while something is due, and
    `AgentRenameAmbiguous` when self-hosted records differ from `old` only in
    case: nothing is renamed and the caller skips the agent pass, so that agent is never adopted.
    """
    olds = _named(agents, old)
    legacy = [agent for agent in olds if _managed(agent)]
    readers = _legacy_readers(agents, old=old, new=new, old_agent_live=len(olds) > len(legacy))
    if (legacy or readers) and _only_unmanaged(_named(agents, new)):
        raise AgentRenameBlocked(old=old, new=new)
    selfhosted = await inspect_selfhosted_rename(session, tenant_id=tenant_id, old=old, new=new)
    _raise_if_selfhosted_blocked(selfhosted, old=old, new=new)
    if not legacy and not readers and selfhosted == "absent":
        return None
    first = legacy[0].id if legacy else None
    outcome = ResourceOutcome(kind="agent", name=new, action=Action.RENAMED, anthropic_id=first)
    return SeededRename(old=old, new=new, moves=(*legacy, *readers), outcome=outcome)


async def write_seeded_rename(
    session: AsyncSession, rename: SeededRename, *, tenant_id: uuid.UUID
) -> None:
    """The database half of `rename`, under the caller's reconcile lock. Idempotent."""
    old, new = rename.old, rename.new
    selfhosted = await rename_selfhosted_agent(session, tenant_id=tenant_id, old=old, new=new)
    _raise_if_selfhosted_blocked(selfhosted, old=old, new=new)
    await rename_agent_references(session, tenant_id=tenant_id, old=old, new=new)


async def move_seeded_rename(
    client: AsyncAnthropic | None,
    session_factory: async_sessionmaker[AsyncSession],
    rename: SeededRename,
    *,
    tenant_id: uuid.UUID,
) -> None:
    """The provider half of `rename`, then its access rules. Idempotent."""
    if client is not None:
        for agent in rename.moves:
            await _rename_on_provider(
                client, agent, tenant_id=tenant_id, old=rename.old, new=rename.new
            )
    async with session_factory() as session, session.begin():
        await rename_policy_keys(session, tenant_id=tenant_id, old=rename.old, new=rename.new)
    _log.info("defaults.agent_renamed", tenant_id=str(tenant_id), old=rename.old, new=rename.new)


async def rename_seeded_agent(
    client: AsyncAnthropic | None,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    old: str,
    new: str,
    lock_key: str,
    dry_run: bool,
) -> ResourceOutcome | None:
    """Rename the tenant's seeded `old` to `new`; None when there is nothing to do.

    Plans without the lock, so a tenant with nothing due never waits on it, then
    re-plans under it. Without a `client` (no Anthropic credentials) only
    self-hosted records move. Raises as `plan_seeded_rename` does.
    """

    async def _plan(session: AsyncSession) -> SeededRename | None:
        agents = [] if client is None else await list_agents_by_tenant(client, tenant_id=tenant_id)
        return await plan_seeded_rename(agents, session, tenant_id=tenant_id, old=old, new=new)

    async with session_factory() as session:
        rename = await _plan(session)
    if rename is None or dry_run:
        return None if rename is None else rename.outcome
    async with session_factory() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": lock_key}
        )
        rename = await _plan(session)  # another apply may have moved first
        if rename is None:
            return None
        await write_seeded_rename(session, rename, tenant_id=tenant_id)
    await move_seeded_rename(client, session_factory, rename, tenant_id=tenant_id)
    return rename.outcome


def _raise_if_selfhosted_blocked(state: SelfHostedState, *, old: str, new: str) -> None:
    if state == "taken":
        raise AgentRenameBlocked(old=old, new=new)
    if state == "ambiguous":
        raise AgentRenameAmbiguous(old=old)
