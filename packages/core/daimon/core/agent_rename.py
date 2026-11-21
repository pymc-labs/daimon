"""Rename a tenant's own agent, keeping its id, and move every name that points at it.

The operator path (`daimon agents rename`). The agent keeps its provider id, so
what is keyed by that id (stored per-agent MCP credentials, memory, sessions)
keeps working. Names move in three places: the live name references in the
database (`daimon.core.stores.agent_rename`), the access policy's per-agent
rules, and the provider agent's `name` and `daimon_name` (with its report
readers). It reuses the defaults rename's helpers.

`replace_seeded` frees the name when the defaults pipeline holds it: the seeded,
Daimon-managed agent (and its readers) take a retired name on the provider and
are archived there. Nothing in the database names that retired agent, so the
scope rows that named the seeded agent now name the renamed one. This never
calls `forget_archived_agent`, which would clear those rows.

Order, all under the tenant's defaults reconcile lock, so no `defaults apply`
runs between the steps (it would create a second `default`). The lock is the
same transaction-level advisory lock `defaults apply` takes, held by the
rename's own transaction. The undo runs inside that transaction, before it
rolls back, so the lock covers it (a separate session lock would need a second
connection, which single-connection pools lack). Under it: plan again from a
fresh listing and refuse on collisions, write the database references, retire
the seeded agent, rename the agent, commit. A failure before the commit rolls
the database back and renames the provider agents back (each rename is noted
before its call, and an undo of one that never happened does nothing). The
archive of the retired seeded agent comes after the commit, because it cannot
be undone; if it fails, the next `defaults apply` sweeps it, as it is managed
and no longer in the tree.

The seeded agent's report readers are renamed to the retired name but not
archived: published reports hold tokens bound to the reader's agent id. They
are not managed, so `defaults apply` never touches them.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field, replace

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.anthropic_guard import anthropic_configured
from daimon.core.defaults._reconcile import reconcile_lock_key
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_READER_OF
from daimon.core.defaults.rename_agents import (
    agents_named,
    is_managed,
    rename_on_provider,
    source_readers,
)
from daimon.core.errors import DaimonError
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import archive_agent
from daimon.core.stores.agent_avatars import normalize_agent_name
from daimon.core.stores.agent_rename import (
    agent_name_collisions,
    count_agent_references,
    policy_keys_to_move,
    rename_agent_references,
    rename_policy_keys,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


class AgentRenameRefused(DaimonError):
    """The rename would break something; nothing was changed."""


@dataclass(frozen=True)
class AgentRenamePlan:
    """What a rename of the tenant's `old` to `new` touches, from one listing."""

    tenant_id: uuid.UUID
    old: str
    new: str
    agent: BetaManagedAgentsAgent
    readers: tuple[BetaManagedAgentsAgent, ...]
    # The seeded agent that `replace_seeded` renames and archives; empty otherwise.
    seeded: tuple[BetaManagedAgentsAgent, ...]
    retired_name: str | None
    # Its report readers: renamed with it, kept live.
    seeded_readers: tuple[BetaManagedAgentsAgent, ...] = ()
    # Rows that would move, per `table.column`, and the policy keys that would move.
    references: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    policy_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentRenameResult:
    plan: AgentRenamePlan
    # Retired agents whose archive failed; the next `defaults apply` sweeps them.
    unarchived: tuple[str, ...] = ()


def _retired_name(new: str, today: dt.date) -> str:
    return f"{new}-seeded-retired-{today:%Y%m%d}"


def plan_from_listing(
    agents: Collection[BetaManagedAgentsAgent],
    *,
    tenant_id: uuid.UUID,
    old: str,
    new: str,
    replace_seeded: bool,
    today: dt.date,
) -> AgentRenamePlan:
    """The rename `agents` allow; raises `AgentRenameRefused` when it is unsafe."""
    if normalize_agent_name(old) == normalize_agent_name(new):
        raise AgentRenameRefused(f"{old!r} and {new!r} are the same name.")
    sources = [a for a in agents_named(agents, old) if MA_METADATA_KEY_READER_OF not in a.metadata]
    if not sources:
        raise AgentRenameRefused(f"This workspace has no agent named {old!r}.")
    if len(sources) > 1:
        raise AgentRenameRefused(
            f"This workspace has more than one agent named {old!r}. Archive the extra ones first."
        )
    agent = sources[0]
    if is_managed(agent):
        raise AgentRenameRefused(
            f"{old!r} is a built-in agent that Daimon keeps in sync, so it cannot be renamed."
        )
    holders = [a for a in agents_named(agents, new) if MA_METADATA_KEY_READER_OF not in a.metadata]
    if any(not is_managed(a) for a in holders):
        raise AgentRenameRefused(
            f"This workspace already has its own agent named {new!r}. Rename or archive it first."
        )
    seeded: list[BetaManagedAgentsAgent] = []
    seeded_readers: list[BetaManagedAgentsAgent] = []
    retired_name = None
    if holders:
        if not replace_seeded:
            raise AgentRenameRefused(
                f"The built-in agent holds the name {new!r}. "
                "Pass --replace-seeded to retire it in this workspace and use the name."
            )
        retired_name = _retired_name(new, today)
        if agents_named(agents, retired_name):
            raise AgentRenameRefused(f"An agent named {retired_name!r} already exists.")
        seeded = list(holders)
        seeded_readers = source_readers(agents, new)
    readers = source_readers(agents, old)
    leftover = [
        a
        for a in agents_named(agents, f"{new}-reader")
        if not any(a.id == s.id for s in seeded_readers)
    ]
    if readers and leftover:
        raise AgentRenameRefused(
            f"An agent named '{new}-reader' already exists, and {old!r} has a reader of its own."
        )
    return AgentRenamePlan(
        tenant_id=tenant_id,
        old=old,
        new=new,
        agent=agent,
        readers=tuple(readers),
        seeded=tuple(seeded),
        seeded_readers=tuple(seeded_readers),
        retired_name=retired_name,
    )


async def _refuse_collisions(session: AsyncSession, plan: AgentRenamePlan) -> None:
    found = await agent_name_collisions(
        session, tenant_id=plan.tenant_id, old=plan.old, new=plan.new
    )
    if found:
        listing = "; ".join(f"{label}: {count}" for label, count in found.items())
        raise AgentRenameRefused(
            f"Both {plan.old!r} and {plan.new!r} have entries in: {listing}. "
            "Remove one side's entries first; the rename does not merge them."
        )


async def _list(client: AsyncAnthropic, tenant_id: uuid.UUID) -> list[BetaManagedAgentsAgent]:
    return await list_agents_by_tenant(client, tenant_id=tenant_id)


async def plan_agent_rename(
    client: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    old: str,
    new: str,
    replace_seeded: bool,
    today: dt.date | None = None,
) -> AgentRenamePlan:
    """The plan with its database counts; reads only."""
    plan = plan_from_listing(
        await _list(client, tenant_id),
        tenant_id=tenant_id,
        old=old,
        new=new,
        replace_seeded=replace_seeded,
        today=today or dt.datetime.now(dt.UTC).date(),
    )
    async with session_factory() as session:
        await _refuse_collisions(session, plan)
        references = await count_agent_references(session, tenant_id=tenant_id, old=old, new=new)
        keys = await policy_keys_to_move(session, tenant_id=tenant_id, old=old, new=new)
    return replace(plan, references=references, policy_keys=tuple(keys))


async def rename_user_agent(
    client: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    old: str,
    new: str,
    replace_seeded: bool = False,
    dry_run: bool = False,
    today: dt.date | None = None,
) -> AgentRenameResult:
    """Rename the tenant's own agent `old` to `new`, as the module docstring orders it.

    With `dry_run` it plans and returns, writing nothing.
    """
    if not anthropic_configured(client):
        raise AgentRenameRefused("Renaming an agent needs an Anthropic key.")
    day = today or dt.datetime.now(dt.UTC).date()
    plan = await plan_agent_rename(
        client,
        session_factory,
        tenant_id=tenant_id,
        old=old,
        new=new,
        replace_seeded=replace_seeded,
        today=day,
    )
    if dry_run:
        return AgentRenameResult(plan)

    done: list[tuple[BetaManagedAgentsAgent, str, str]] = []

    async def _move(agent: BetaManagedAgentsAgent, *, src: str, dst: str) -> None:
        done.append((agent, src, dst))  # before the call: one that lands but times out is undone
        await rename_on_provider(client, agent, tenant_id=tenant_id, old=src, new=dst)

    async with session_factory() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": reconcile_lock_key(tenant_id)},
        )
        try:
            plan = plan_from_listing(
                await _list(client, tenant_id),
                tenant_id=tenant_id,
                old=old,
                new=new,
                replace_seeded=replace_seeded,
                today=day,
            )
            await _refuse_collisions(session, plan)
            await rename_agent_references(session, tenant_id=tenant_id, old=old, new=new)
            await rename_policy_keys(session, tenant_id=tenant_id, old=old, new=new)
            if plan.retired_name is not None:
                for agent in (*plan.seeded, *plan.seeded_readers):
                    await _move(agent, src=new, dst=plan.retired_name)
            for agent in (plan.agent, *plan.readers):
                await _move(agent, src=old, dst=new)
        except BaseException:
            # Still inside the transaction, so the lock is held until the undo is done.
            await _move_back(client, done, tenant_id=tenant_id)
            raise
    _log.info("agents.renamed", tenant_id=str(tenant_id), old=old, new=new, agent_id=plan.agent.id)
    unarchived = await _archive_retired(client, plan)
    return AgentRenameResult(plan, tuple(unarchived))


async def _move_back(
    client: AsyncAnthropic,
    done: list[tuple[BetaManagedAgentsAgent, str, str]],
    *,
    tenant_id: uuid.UUID,
) -> None:
    """Undo the provider renames of a rename that did not commit, newest first."""
    for agent, src, dst in reversed(done):
        try:
            await rename_on_provider(client, agent, tenant_id=tenant_id, old=dst, new=src)
        except Exception:
            _log.exception("agents.rename_undo_failed", agent_id=agent.id, name=dst, restore=src)


async def _archive_retired(client: AsyncAnthropic, plan: AgentRenamePlan) -> list[str]:
    failed: list[str] = []
    for agent in plan.seeded:
        try:
            await archive_agent(
                client, agent.id, scope=resource_scope(tenant_id=str(plan.tenant_id))
            )
        except Exception:
            _log.exception("agents.retired_archive_failed", agent_id=agent.id)
            failed.append(agent.id)
    return failed
