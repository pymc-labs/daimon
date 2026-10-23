"""Lazy provisioning + session mounting of MA memory stores.

Two kinds of store, both created lazily and bound in Postgres:

- **shared**: one per (tenant, agent), in `agent_memory_store`. Every session of
  the agent mounts it, whoever is talking and wherever. It holds team and agent
  knowledge: conventions, project facts, how tools and skills behave.
- **personal**: one per (tenant, agent, account), in
  `agent_personal_memory_store`. Only that person's own interactive sessions
  mount it (a chat session is already per caller, keyed on the account), so
  another person's session cannot read it — the isolation is the mount, not
  the prompt. It holds that person's preferences and facts about them.
- **channel**: one per (tenant, agent, platform, parent channel), in
  `agent_channel_memory_store`. A held turn (a requester from another
  organisation, or a channel shared with one) mounts only this store: never the
  shared store and never a personal one, and an unheld turn never mounts it.
  Everyone in the channel sees it, so it holds what the agent would say there.

`ensure_memory_store_and_mount` / `ensure_personal_memory_store_and_mount` /
`ensure_channel_memory_store_and_mount` return the session `resources[]` entry;
callers (sessions.create_session) treat failures as degrade-not-block: a
session without memory beats no session.

Tenant isolation: all reads/writes are keyed (tenant_id, agent_id[, account_id]);
the MA store is stamped with daimon_tenant/daimon_agent(/daimon_account)
metadata for traceability (stores are workspace-scoped on Anthropic's side —
the DB binding is the isolation boundary).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anthropic as anthropic_errors
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta.beta_managed_agents_memory_store_resource_param import (
    BetaManagedAgentsMemoryStoreResourceParam,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT, MA_METADATA_KEY_TENANT
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import (
    archive_memory_store,
    create_memory_store,
    delete_memory_store,
    list_memory_entries,
    read_memory,
    write_memory,
)
from daimon.core.stores.agent_memory_stores import (
    clear_channel_memory_store,
    clear_memory_store,
    clear_personal_memory_store,
    get_channel_memory_store_id,
    get_memory_store_id,
    get_personal_memory_store_id,
    insert_channel_memory_store,
    insert_memory_store,
    insert_personal_memory_store,
    list_channel_memory_stores_for_agent,
    list_erased_accounts_with_personal_memory,
    list_personal_memory_stores_for_account,
    list_personal_memory_stores_for_agent,
    mark_personal_memory_store_archived,
)
from daimon.core.turn_hold import TurnHold
from mux.contracts.ids import Scope
from mux.errors import ScopeViolation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

MA_METADATA_KEY_AGENT = "daimon_agent"
MA_METADATA_KEY_PLATFORM = "daimon_platform"
MA_METADATA_KEY_CHANNEL = "daimon_channel"

MEMORY_INSTRUCTIONS = (
    "This is the agent's shared memory: everyone who talks to this agent, in "
    "every channel, sees it. Check it before starting any task. Record durable "
    "team and agent knowledge here: project conventions, decisions, facts about "
    "the work, how tools and skills behave, and corrections to how you work. "
    "Never record anything personal here: a person's preferences or facts about "
    "a specific person go in that person's personal memory, and nothing from a "
    "personal memory is ever copied into this one. Keep files small and focused "
    "(one topic per file); update existing files instead of appending "
    "duplicates. Do not store secrets, credentials, or transient conversation "
    "details."
)

SHARED_ONLY_MEMORY_INSTRUCTIONS = (
    "This is the agent's shared memory: everyone who talks to this agent, in "
    "every channel, sees it. Check it before starting any task. Record durable "
    "team and agent knowledge here: project conventions, decisions, facts about "
    "the work, how tools and skills behave, and corrections to how you work. "
    "No personal memory is mounted in this conversation, so do not record a "
    "person's preferences or facts about a specific person anywhere. Keep files "
    "small and focused (one topic per file); update existing files instead of "
    "appending duplicates. Do not store secrets, credentials, or transient "
    "conversation details."
)

READ_ONLY_MEMORY_INSTRUCTIONS = (
    "This is shared persistent memory, mounted read-only for this conversation. "
    "Consult it when useful, but do not create, update, rename, or delete memory files. "
    "Do not attempt to save facts from this turn into persistent memory."
)

PERSONAL_MEMORY_INSTRUCTIONS = (
    "This is the personal memory of the person you are talking with in this "
    "conversation. Only their own conversations with this agent see it. Check it "
    "before starting a task for them. Record their durable preferences (format, "
    "tone, tools, working habits) and facts about them that they would expect "
    "you to remember. Team and project knowledge belongs in the shared memory, "
    "and facts about anyone else do not belong here. Never copy anything from "
    "this memory into the shared memory, and do not quote it in a reply other "
    "people will read unless this person asks. Keep files small and focused; "
    "update existing files instead of appending duplicates. Do not store "
    "secrets, credentials, or transient conversation details."
)

READ_ONLY_PERSONAL_MEMORY_INSTRUCTIONS = (
    "This is the personal memory of the person you are talking with, mounted "
    "read-only for this conversation. Consult it to follow their preferences, "
    "but do not create, update, rename, or delete files in it, never copy "
    "anything from it into another memory, and do not quote it anywhere other "
    "people will read it."
)

CHANNEL_MEMORY_INSTRUCTIONS = (
    "This is the memory of this channel. Everyone in this channel sees it, "
    "including people from another organisation, and nobody outside this "
    "channel does. Check it before starting any task. Record durable knowledge "
    "about the work done here: decisions, conventions, facts the channel "
    "agreed on. Never record anything you would not say in this channel: no "
    "internal material, no other channel's or person's private notes, no "
    "secrets, credentials or transient conversation details. Keep files small "
    "and focused (one topic per file); update existing files instead of "
    "appending duplicates."
)

READ_ONLY_CHANNEL_MEMORY_INSTRUCTIONS = (
    "This is the memory of this channel, mounted read-only for this "
    "conversation. Everyone in the channel, including people from another "
    "organisation, sees it. Consult it when useful, but do not create, update, "
    "rename, or delete memory files."
)

_CHANNEL_STORE_DESCRIPTION = (
    "Memory of one channel for the daimon agent '{agent_name}'. Seen by everyone in "
    "that channel, including people from another organisation; managed by daimon."
)

_STORE_DESCRIPTION = (
    "Persistent memory for the daimon agent '{agent_name}'. Written by the "
    "agent itself across sessions; managed by daimon."
)

_PERSONAL_STORE_DESCRIPTION = (
    "Personal memory of one person for the daimon agent '{agent_name}': their "
    "preferences and facts about them. Mounted only in that person's own "
    "conversations; managed by daimon."
)


async def _ensure_store(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scope: Scope,
    read_binding: Callable[[AsyncSession], Awaitable[str | None]],
    insert_binding: Callable[[AsyncSession, str], Awaitable[str]],
    name: str,
    description: str,
    metadata: dict[str, str],
) -> str:
    """The bound store id, creating the store and its binding on first use.

    Warm path: one DB read, zero API calls. Cold path: one
    memory_stores.create + race-safe insert; on a lost race the just-created
    orphan store is deleted and the winner's id is used.
    """
    async with session_factory() as session:
        store_id = await read_binding(session)
    if store_id is not None:
        return store_id

    created = await create_memory_store(
        anthropic,
        {"name": name, "description": description, "metadata": metadata},
        scope=scope,
    )
    try:
        async with session_factory() as session, session.begin():
            store_id = await insert_binding(session, created.id)
    except Exception:
        # The binding never landed, so nothing references the store we
        # just created — delete it, or a DB outage mints one unreferenced
        # active store per session attempt.
        try:
            await delete_memory_store(anthropic, created.id, scope=scope)
        except anthropic_errors.APIError:
            _log.warning("memory_store.orphan_delete_failed", orphan=created.id)
        raise
    if store_id != created.id:
        # Lost the provisioning race — discard the orphan store.
        _log.info("memory_store.race_lost", orphan=created.id, winner=store_id)
        try:
            await delete_memory_store(anthropic, created.id, scope=scope)
        except anthropic_errors.APIError:
            _log.warning("memory_store.orphan_delete_failed", orphan=created.id)
    else:
        _log.info("memory_store.provisioned", memory_store_id=store_id, name=name)
    return store_id


def memory_scope(hold: TurnHold | None) -> str | None:
    """The memory isolation scope a session is built for: None unheld, else its channel.

    Recorded in the session snapshot whether or not a store mounted, so a held
    session whose store failed to provision is still known to be the place's.
    """
    if hold is None:
        return None
    return f"channel:{hold.platform}:{hold.channel_id}"


def memory_instructions(*, read_only: bool, personal_mounted: bool = False) -> str:
    """The shared mount's instructions; writable ones depend on a personal mount beside it."""
    if read_only:
        return READ_ONLY_MEMORY_INSTRUCTIONS
    return MEMORY_INSTRUCTIONS if personal_mounted else SHARED_ONLY_MEMORY_INSTRUCTIONS


def memory_mount(
    store_id: str, *, read_only: bool, personal_mounted: bool = False
) -> BetaManagedAgentsMemoryStoreResourceParam:
    """The same mount intent applies before and after lazy provisioning."""
    return {
        "type": "memory_store",
        "memory_store_id": store_id,
        "access": "read_only" if read_only else "read_write",
        "instructions": memory_instructions(read_only=read_only, personal_mounted=personal_mounted),
    }


async def ensure_memory_store_and_mount(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_name: str,
    read_only: bool = False,
    personal_mounted: bool = False,
    expected_memory_store_id: str | None = None,
) -> BetaManagedAgentsMemoryStoreResourceParam:
    """Return the shared memory-store session resource, provisioning on first use.

    `personal_mounted` says whether the same session also mounts the caller's
    personal store: the writable instructions then point personal facts there,
    and otherwise forbid recording them at all. Instructions are per attachment
    and frozen at session creation, so a wording change reaches only new
    sessions and never makes a live one read as drifted.
    An expected ID requires that exact warm binding, without provisioning.
    """

    async def read(session: AsyncSession) -> str | None:
        store_id = await get_memory_store_id(session, tenant_id=tenant_id, agent_id=agent_id)
        if expected_memory_store_id is not None and (
            not expected_memory_store_id or store_id != expected_memory_store_id
        ):
            raise ScopeViolation("memory-store", "captured warm binding differs")
        return store_id

    async def insert(session: AsyncSession, store_id: str) -> str:
        return await insert_memory_store(
            session, tenant_id=tenant_id, agent_id=agent_id, memory_store_id=store_id
        )

    store_id = await _ensure_store(
        anthropic,
        session_factory,
        scope=resource_scope(tenant_id=str(tenant_id)),
        read_binding=read,
        insert_binding=insert,
        name=f"daimon {agent_name} {tenant_id}",
        description=_STORE_DESCRIPTION.format(agent_name=agent_name),
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_AGENT: str(agent_id),
        },
    )
    return memory_mount(store_id, read_only=read_only, personal_mounted=personal_mounted)


async def ensure_personal_memory_store_and_mount(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_name: str,
    read_only: bool = False,
) -> BetaManagedAgentsMemoryStoreResourceParam:
    """Return the caller's personal memory-store session resource, provisioning on first use.

    The store is named `personal <account id>`, so it mounts at
    `/mnt/memory/personal-<account id>/` beside the shared store. Its access
    follows the shared mount's: a sealed channel or a read-only DM keeps both
    read-only, or a sealed conversation could be written into memory that the
    same person's other conversations read.
    """

    async def read(session: AsyncSession) -> str | None:
        return await get_personal_memory_store_id(
            session, tenant_id=tenant_id, agent_id=agent_id, account_id=account_id
        )

    async def insert(session: AsyncSession, store_id: str) -> str:
        return await insert_personal_memory_store(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account_id,
            memory_store_id=store_id,
        )

    store_id = await _ensure_store(
        anthropic,
        session_factory,
        scope=resource_scope(tenant_id=str(tenant_id)),
        read_binding=read,
        insert_binding=insert,
        name=f"personal {account_id}",
        description=_PERSONAL_STORE_DESCRIPTION.format(agent_name=agent_name),
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_AGENT: str(agent_id),
            MA_METADATA_KEY_ACCOUNT: str(account_id),
        },
    )
    return {
        "type": "memory_store",
        "memory_store_id": store_id,
        "access": "read_only" if read_only else "read_write",
        "instructions": (
            READ_ONLY_PERSONAL_MEMORY_INSTRUCTIONS if read_only else PERSONAL_MEMORY_INSTRUCTIONS
        ),
    }


async def ensure_channel_memory_store_and_mount(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: str,
    channel_id: str,
    agent_name: str,
    read_only: bool = False,
) -> BetaManagedAgentsMemoryStoreResourceParam:
    """Return a held channel's memory-store session resource, provisioning on first use.

    The only memory a held session mounts. `channel_id` is the parent channel,
    so a thread and its channel share one store. Access follows the turn's
    memory restriction like the other mounts.
    """

    async def read(session: AsyncSession) -> str | None:
        return await get_channel_memory_store_id(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            platform=platform,
            channel_id=channel_id,
        )

    async def insert(session: AsyncSession, store_id: str) -> str:
        return await insert_channel_memory_store(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            platform=platform,
            channel_id=channel_id,
            memory_store_id=store_id,
        )

    store_id = await _ensure_store(
        anthropic,
        session_factory,
        scope=resource_scope(tenant_id=str(tenant_id)),
        read_binding=read,
        insert_binding=insert,
        name=f"channel {platform} {channel_id[:64]}",
        description=_CHANNEL_STORE_DESCRIPTION.format(agent_name=agent_name),
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_AGENT: str(agent_id),
            MA_METADATA_KEY_PLATFORM: platform,
            MA_METADATA_KEY_CHANNEL: channel_id[:256],
        },
    )
    return {
        "type": "memory_store",
        "memory_store_id": store_id,
        "access": "read_only" if read_only else "read_write",
        "instructions": (
            READ_ONLY_CHANNEL_MEMORY_INSTRUCTIONS if read_only else CHANNEL_MEMORY_INSTRUCTIONS
        ),
    }


# Ceiling on the memories one copy takes, so a fork stays one tool call.
COPY_MEMORY_LIMIT = 500
# Memories read and written at once while copying.
COPY_MEMORY_CONCURRENCY = 8


@dataclass(frozen=True)
class CopiedMemory:
    copied: int
    """Memories written into the copy's shared store."""
    left: int = 0
    """Memories past `COPY_MEMORY_LIMIT`, not copied."""
    failed: int = 0
    """Memories whose read or write failed, not copied."""


async def copy_shared_memory(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    source_agent_id: uuid.UUID,
    copy_agent_id: uuid.UUID,
    copy_name: str,
) -> CopiedMemory | None:
    """Copy the source's shared memory into a new copy's own shared store.

    Only the shared store: it is already seen by everyone who talks to the
    source. Personal and channel stores are never copied. None when the source
    has no shared store yet, which costs one DB read and no API call. Up to
    `COPY_MEMORY_LIMIT` memories, `COPY_MEMORY_CONCURRENCY` at a time; one
    that fails is counted in `failed` and the rest still go. Raises only when
    the source can't be listed or the copy's store can't be made, before
    anything is written.
    """
    async with session_factory() as session:
        source_store = await get_memory_store_id(
            session, tenant_id=tenant_id, agent_id=source_agent_id
        )
    if source_store is None:
        return None
    scope = resource_scope(tenant_id=str(tenant_id))
    entries = [
        item
        async for item in list_memory_entries(anthropic, source_store, path_prefix="/", scope=scope)
        if item.type == "memory"
    ]
    if not entries:
        return CopiedMemory(0)
    mount = await ensure_memory_store_and_mount(
        anthropic,
        session_factory,
        tenant_id=tenant_id,
        agent_id=copy_agent_id,
        agent_name=copy_name,
    )
    target = mount["memory_store_id"]
    gate = asyncio.Semaphore(COPY_MEMORY_CONCURRENCY)

    async def copy_one(memory_id: str, path: str) -> bool:
        async with gate:
            try:
                memory = await read_memory(anthropic, source_store, memory_id, scope=scope)
                await write_memory(anthropic, target, path, memory.content or "", scope=scope)
            except Exception as error:
                # Best effort per memory: the receipt counts what didn't come.
                _log.warning(
                    "memory_store.copy_one_failed", error=type(error).__name__, exc_info=True
                )
                return False
            return True

    results = await asyncio.gather(
        *(copy_one(item.id, item.path) for item in entries[:COPY_MEMORY_LIMIT])
    )
    copied = sum(results)
    _log.info(
        "memory_store.copied",
        tenant_id=str(tenant_id),
        source_agent_id=str(source_agent_id),
        copy_agent_id=str(copy_agent_id),
        count=copied,
    )
    return CopiedMemory(
        copied,
        left=max(0, len(entries) - COPY_MEMORY_LIMIT),
        failed=len(results) - copied,
    )


async def archive_memory_store_for_agent(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> None:
    """Archive the agent's stores (audit trail preserved) and retire their bindings.

    The shared store, every held channel's store and every person's personal
    store for the agent go together: a personal store belongs to its agent and
    is never mounted once the agent is gone. The shared and channel bindings
    are dropped once their store is archived. A personal
    binding is only stamped archived: the archived store still holds that
    person's memories, and account erasure finds it by that row.

    Each store is archived on its own, so one failure does not leave the rest
    live; the first API error is re-raised once every store has been tried.
    Idempotent: unbound agent → no-op; already-archived/missing store on the
    MA side → binding still retired.
    """
    backend_scope = resource_scope(tenant_id=str(tenant_id))
    async with session_factory() as session:
        store_id = await get_memory_store_id(session, tenant_id=tenant_id, agent_id=agent_id)
        personal = await list_personal_memory_stores_for_agent(
            session, tenant_id=tenant_id, agent_id=agent_id
        )
        channels = await list_channel_memory_stores_for_agent(
            session, tenant_id=tenant_id, agent_id=agent_id
        )
    failure: anthropic_errors.APIError | None = None
    for platform, channel_id, channel_store_id in channels:
        try:
            await archive_memory_store(anthropic, channel_store_id, scope=backend_scope)
        except anthropic_errors.NotFoundError:
            _log.info("memory_store.archive_missing", memory_store_id=channel_store_id)
        except anthropic_errors.APIError as error:
            _log.warning(
                "memory_store.channel_archive_failed",
                memory_store_id=channel_store_id,
                error=type(error).__name__,
            )
            failure = failure or error
            continue
        async with session_factory() as session, session.begin():
            await clear_channel_memory_store(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                platform=platform,
                channel_id=channel_id,
            )
        _log.info("memory_store.channel_archived", memory_store_id=channel_store_id)
    for account_id, personal_id in personal:
        try:
            await archive_memory_store(anthropic, personal_id, scope=backend_scope)
        except anthropic_errors.NotFoundError:
            _log.info("memory_store.archive_missing", memory_store_id=personal_id)
        except anthropic_errors.APIError as error:
            _log.warning(
                "memory_store.personal_archive_failed",
                memory_store_id=personal_id,
                error=type(error).__name__,
            )
            failure = failure or error
            continue
        async with session_factory() as session, session.begin():
            await mark_personal_memory_store_archived(
                session, tenant_id=tenant_id, agent_id=agent_id, account_id=account_id
            )
        _log.info("memory_store.personal_archived", memory_store_id=personal_id)
    if store_id is not None:
        shared_archived = True
        try:
            await archive_memory_store(anthropic, store_id, scope=backend_scope)
        except anthropic_errors.NotFoundError:
            _log.info("memory_store.archive_missing", memory_store_id=store_id)
        except anthropic_errors.APIError as error:
            failure = failure or error
            shared_archived = False
        if shared_archived:
            async with session_factory() as session, session.begin():
                await clear_memory_store(session, tenant_id=tenant_id, agent_id=agent_id)
            _log.info("memory_store.archived", memory_store_id=store_id)
    if failure is not None:
        raise failure


async def delete_personal_memory_stores_for_account(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: uuid.UUID,
) -> bool:
    """Delete every personal store one person has, for account erasure.

    Archived stores (their agent was archived) included. Deleted, not archived:
    an archived store keeps its memories and versions, and erasure has to
    remove them. Each binding is dropped only after its
    store is gone (or already missing), so a failure leaves the binding as the
    record of what is still to delete and a retry picks it up. Returns True
    when nothing is left.
    """
    async with session_factory() as session:
        bound = await list_personal_memory_stores_for_account(session, account_id=account_id)
    complete = True
    for tenant_id, agent_id, store_id in bound:
        try:
            await delete_memory_store(
                anthropic, store_id, scope=resource_scope(tenant_id=str(tenant_id))
            )
        except anthropic_errors.NotFoundError:
            pass
        except anthropic_errors.APIError as error:
            _log.warning(
                "memory_store.personal_delete_failed",
                memory_store_id=store_id,
                error=type(error).__name__,
            )
            complete = False
            continue
        async with session_factory() as session, session.begin():
            await clear_personal_memory_store(
                session, tenant_id=tenant_id, agent_id=agent_id, account_id=account_id
            )
        _log.info("memory_store.personal_deleted", memory_store_id=store_id)
    return complete


async def sweep_erased_personal_memory(
    anthropic: AsyncAnthropic, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Finish deleting the personal stores of accounts already erased.

    Independent of the session-deletion work item, so an account erased with
    no principal left, without an Anthropic client, or whose upstream delete
    failed is still cleaned up: its bindings outlive the account row.
    """
    async with session_factory() as session:
        accounts = await list_erased_accounts_with_personal_memory(session)
    for account_id in accounts:
        if not await delete_personal_memory_stores_for_account(
            anthropic, session_factory, account_id=account_id
        ):
            _log.warning("memory_store.personal_erasure_pending", account_id=str(account_id))
