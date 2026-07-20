"""Real-DB behavior tests for the agent_memory_stores store."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.stores.agent_memory_stores import (
    clear_memory_store,
    get_memory_store_id,
    insert_memory_store,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def test_get_returns_none_when_unbound(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    result = await get_memory_store_id(
        db_session, tenant_id=tenant.id, agent_id=uuid.uuid4()
    )
    assert result is None


async def test_insert_then_get_roundtrip(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    won = await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_id, memory_store_id="memstore_A"
    )
    assert won == "memstore_A", "first insert must win with its own id"
    got = await get_memory_store_id(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert got == "memstore_A"


async def test_insert_conflict_returns_existing_id(db_session: AsyncSession) -> None:
    """Race semantics: second insert loses and returns the first id."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_id, memory_store_id="memstore_A"
    )
    won = await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_id, memory_store_id="memstore_B"
    )
    assert won == "memstore_A", "conflict must return the existing binding, not overwrite"


async def test_tenant_isolation(db_session: AsyncSession) -> None:
    """Same agent_id under a different tenant is a distinct binding."""
    t1 = await make_tenant(db_session)
    t2 = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await insert_memory_store(
        db_session, tenant_id=t1.id, agent_id=agent_id, memory_store_id="memstore_T1"
    )
    assert (
        await get_memory_store_id(db_session, tenant_id=t2.id, agent_id=agent_id)
    ) is None


async def test_clear_is_idempotent(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_id, memory_store_id="memstore_A"
    )
    await clear_memory_store(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert (
        await get_memory_store_id(db_session, tenant_id=tenant.id, agent_id=agent_id)
    ) is None
    # second clear must not raise
    await clear_memory_store(db_session, tenant_id=tenant.id, agent_id=agent_id)
