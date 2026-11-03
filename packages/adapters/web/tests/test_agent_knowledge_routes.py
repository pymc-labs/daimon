"""`/api/agents/{id}/knowledge`: shared and channel memory, read-only, scoped to the agent."""

from __future__ import annotations

import uuid

from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.agent_google_binding import upsert_agent_google_binding
from daimon.core.stores.agent_memory_stores import insert_channel_memory_store, insert_memory_store
from mux.testing.drivers.anthropic.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    make_fake_memory_store_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import Agents, agent_client, agent_people, make_admin, sign_in_token


async def _seeded(
    agents: Agents, factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    state = FakeMemoryStoreState()
    handler = make_fake_memory_store_handler(state)
    agents.router.add("GET", r"/v1/memory_stores/.*", lambda req, _m: handler(req))
    client = build_fake_anthropic(handler)
    ids: list[str] = []
    for files in ({"/style.md": "tabs"}, {"/room.md": "weekly"}):
        store = await client.beta.memory_stores.create(name="m", description="d")
        for path, content in files.items():
            await client.beta.memory_stores.memories.create(store.id, path=path, content=content)
        ids.append(store.id)
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_w")
    async with factory.begin() as db:
        await insert_memory_store(
            db, tenant_id=tenant_id, agent_id=agent_id, memory_store_id=ids[0]
        )
        await insert_channel_memory_store(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            platform="discord",
            channel_id="c1",
            memory_store_id=ids[1],
        )


async def test_an_owner_reads_shared_memory_and_only_an_admin_sees_channel_memory(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await agent_people(db_session)
    await _seeded(agents, db_session_factory, owner.tenant_id)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        stores = await client.get("/api/agents/ag_w/knowledge")
        paths = await client.get("/api/agents/ag_w/knowledge/paths", params={"store": "shared"})
        text = await client.get(
            "/api/agents/ag_w/knowledge/content", params={"store": "shared", "path": "/style.md"}
        )
        channel = await client.get(
            "/api/agents/ag_w/knowledge/paths", params={"store": "discord/c1"}
        )
    assert stores.json() == {"shared": True, "channels": []}, "an owner sees no channel memory"
    assert paths.json() == ["/style.md"] and text.json()["content"] == "tabs"
    assert channel.status_code == 403, "channel memory is for admins"

    await make_admin(db_session_factory, owner)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        stores = await client.get("/api/agents/ag_w/knowledge")
        channel = await client.get(
            "/api/agents/ag_w/knowledge/content",
            params={"store": "discord/c1", "path": "/room.md"},
        )
    assert [c["store"] for c in stores.json()["channels"]] == ["discord/c1"]
    assert channel.json()["content"] == "weekly"


async def test_a_stranger_reads_nothing_and_a_missing_memory_is_not_found(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await _seeded(agents, db_session_factory, owner.tenant_id)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, other.tenant_id, db_session_factory, token) as client:
        refused = await client.get("/api/agents/ag_w/knowledge")
    assert refused.status_code == 403, "only the owner or an admin"
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        missing = await client.get(
            "/api/agents/ag_w/knowledge/content", params={"store": "shared", "path": "/nope.md"}
        )
        unknown = await client.get("/api/agents/ag_w/knowledge/paths", params={"store": "x"})
    assert missing.status_code == 404 and unknown.status_code == 404


async def test_the_bound_google_identity_is_shown_to_the_owner_and_not_to_others(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    async with db_session_factory.begin() as db:
        await upsert_agent_google_binding(
            db,
            agent_id=derive_agent_uuid(tenant_id=owner.tenant_id, ma_agent_id="ag_w"),
            email="bot@example.com",
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        bound = await client.get("/api/agents/ag_w/google")
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, other.tenant_id, db_session_factory, token) as client:
        refused = await client.get("/api/agents/ag_w/google")
    assert bound.json() == {
        "email": "bot@example.com",
        "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
    }
    assert refused.status_code == 403, "only the owner or an admin"
