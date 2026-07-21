"""Lazy provisioning + session mounting of per-agent MA memory stores.

One memory store per (tenant, agent), created on the agent's first session
and bound in the agent_memory_store table. `ensure_memory_store_and_mount`
returns the session `resources[]` entry; callers (sessions.create_session)
treat failures as degrade-not-block — a session without memory beats no
session.

Tenant isolation: all reads/writes are keyed (tenant_id, agent_id); the MA
store is stamped with daimon_tenant/daimon_agent metadata for traceability
(stores are workspace-scoped on Anthropic's side — the DB binding is the
isolation boundary).
"""

from __future__ import annotations

import uuid

import anthropic as anthropic_errors
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta.beta_managed_agents_memory_store_resource_param import (
    BetaManagedAgentsMemoryStoreResourceParam,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
from daimon.core.stores.agent_memory_stores import (
    clear_memory_store,
    get_memory_store_id,
    insert_memory_store,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

MA_METADATA_KEY_AGENT = "daimon_agent"

MEMORY_INSTRUCTIONS = (
    "This is your persistent memory across all conversations and channels. "
    "Check it before starting any task. Record durable facts: user "
    "preferences, project conventions, decisions, and corrections you were "
    "given. Keep files small and focused (one topic per file); update "
    "existing files instead of appending duplicates. Do not store secrets, "
    "credentials, or transient conversation details."
)

_STORE_DESCRIPTION = (
    "Persistent memory for the daimon agent '{agent_name}'. Written by the "
    "agent itself across sessions; managed by daimon."
)


async def ensure_memory_store_and_mount(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_name: str,
) -> BetaManagedAgentsMemoryStoreResourceParam:
    """Return the memory-store session resource, provisioning on first use.

    Warm path: one DB read, zero API calls. Cold path: one
    memory_stores.create + race-safe insert; on a lost race the just-created
    orphan store is deleted and the winner's id is used.
    """
    async with session_factory() as session:
        store_id = await get_memory_store_id(session, tenant_id=tenant_id, agent_id=agent_id)

    if store_id is None:
        created = await anthropic.beta.memory_stores.create(
            name=f"daimon {agent_name} {tenant_id}",
            description=_STORE_DESCRIPTION.format(agent_name=agent_name),
            metadata={
                MA_METADATA_KEY_TENANT: str(tenant_id),
                MA_METADATA_KEY_AGENT: str(agent_id),
            },
        )
        try:
            async with session_factory() as session, session.begin():
                store_id = await insert_memory_store(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    memory_store_id=created.id,
                )
        except Exception:
            # The binding never landed, so nothing references the store we
            # just created — delete it, or a DB outage mints one unreferenced
            # active store per session attempt.
            try:
                await anthropic.beta.memory_stores.delete(created.id)
            except anthropic_errors.APIError:
                _log.warning("memory_store.orphan_delete_failed", orphan=created.id)
            raise
        if store_id != created.id:
            # Lost the provisioning race — discard the orphan store.
            _log.info("memory_store.race_lost", orphan=created.id, winner=store_id)
            try:
                await anthropic.beta.memory_stores.delete(created.id)
            except anthropic_errors.APIError:
                _log.warning("memory_store.orphan_delete_failed", orphan=created.id)
        else:
            _log.info(
                "memory_store.provisioned",
                memory_store_id=store_id,
                agent_name=agent_name,
            )

    return {
        "type": "memory_store",
        "memory_store_id": store_id,
        "access": "read_write",
        "instructions": MEMORY_INSTRUCTIONS,
    }


async def archive_memory_store_for_agent(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> None:
    """Archive the agent's store (audit trail preserved) and drop the binding.

    Idempotent: unbound agent → no-op; already-archived/missing store on the
    MA side → binding still cleared.
    """
    async with session_factory() as session:
        store_id = await get_memory_store_id(session, tenant_id=tenant_id, agent_id=agent_id)
    if store_id is None:
        return
    try:
        await anthropic.beta.memory_stores.archive(store_id)
    except anthropic_errors.NotFoundError:
        _log.info("memory_store.archive_missing", memory_store_id=store_id)
    async with session_factory() as session, session.begin():
        await clear_memory_store(session, tenant_id=tenant_id, agent_id=agent_id)
    _log.info("memory_store.archived", memory_store_id=store_id)
