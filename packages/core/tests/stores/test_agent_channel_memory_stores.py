"""Real-DB behavior tests for the held-channel memory store bindings."""

from __future__ import annotations

import uuid

from daimon.core.stores.agent_memory_stores import (
    clear_channel_memory_store,
    get_channel_memory_store_id,
    insert_channel_memory_store,
    list_channel_memory_stores,
    list_channel_memory_stores_for_agent,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_get_returns_none_when_unbound(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    assert (
        await get_channel_memory_store_id(
            db_session,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            platform="slack",
            channel_id="C-SYN-1",
        )
        is None
    )


async def test_insert_roundtrip_and_first_writer_wins(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    key = {
        "tenant_id": tenant.id,
        "agent_id": agent_id,
        "platform": "slack",
        "channel_id": "C-SYN-1",
    }

    first = await insert_channel_memory_store(db_session, **key, memory_store_id="memstore_A")
    second = await insert_channel_memory_store(db_session, **key, memory_store_id="memstore_B")

    assert (first, second) == ("memstore_A", "memstore_A"), "a conflict returns the winner"
    assert await get_channel_memory_store_id(db_session, **key) == "memstore_A"


async def test_a_binding_is_per_agent_platform_and_channel(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    keys = [
        (agent_id, "slack", "C-SYN-1"),
        (agent_id, "slack", "C-SYN-2"),
        (agent_id, "teams", "C-SYN-1"),
        (uuid.uuid4(), "slack", "C-SYN-1"),
    ]
    for index, (agent, platform, channel) in enumerate(keys):
        won = await insert_channel_memory_store(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent,
            platform=platform,
            channel_id=channel,
            memory_store_id=f"memstore_{index}",
        )
        assert won == f"memstore_{index}", "no other binding is touched by this key"

    assert await list_channel_memory_stores_for_agent(
        db_session, tenant_id=tenant.id, agent_id=agent_id
    ) == [
        ("slack", "C-SYN-1", "memstore_0"),
        ("slack", "C-SYN-2", "memstore_1"),
        ("teams", "C-SYN-1", "memstore_2"),
    ]
    assert len(await list_channel_memory_stores(db_session, tenant_id=tenant.id)) == 4


async def test_list_for_a_tenant_leaves_other_tenants_out(db_session: AsyncSession) -> None:
    mine, theirs = await make_tenant(db_session), await make_tenant(db_session)
    for tenant in (mine, theirs):
        await insert_channel_memory_store(
            db_session,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            platform="slack",
            channel_id="C-SYN-1",
            memory_store_id=f"memstore_{tenant.id}",
        )

    rows = await list_channel_memory_stores(db_session, tenant_id=mine.id)

    assert [row[0] for row in rows] == [mine.id]
    assert {row[0] for row in await list_channel_memory_stores(db_session)} >= {mine.id, theirs.id}


async def test_clear_is_idempotent_and_leaves_other_channels(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    for channel in ("C-SYN-1", "C-SYN-2"):
        await insert_channel_memory_store(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            platform="slack",
            channel_id=channel,
            memory_store_id=f"memstore_{channel}",
        )

    for _ in range(2):
        await clear_channel_memory_store(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            platform="slack",
            channel_id="C-SYN-1",
        )

    assert await list_channel_memory_stores_for_agent(
        db_session, tenant_id=tenant.id, agent_id=agent_id
    ) == [("slack", "C-SYN-2", "memstore_C-SYN-2")]
