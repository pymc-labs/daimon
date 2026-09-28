"""The chat `memory` commands' read path: which store answers in a channel, and its contents."""

from __future__ import annotations

from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.memory_view import (
    get_channel_memory_store,
    get_memory_content,
    list_memory_paths,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_memory_stores import insert_memory_store
from daimon.core.stores.identity import find_platform_principal
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    combine_handlers,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_DEFAULT = DeploymentDefault(agent_name="daimon", environment_name="default")


async def test_channel_store_resolves_the_configured_agent_and_reads_its_memories(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="teams", workspace_id="org-1")
    client = build_fake_anthropic(
        combine_handlers(
            make_fake_memory_store_handler(FakeMemoryStoreState()), make_fake_ma_handler()
        )
    )
    agent = await client.beta.agents.create(
        name="daimon",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "daimon"},
    )
    store = await client.beta.memory_stores.create(name="m", description="d")
    for path, content in {"/b.md": "beta", "/a.md": "alpha"}.items():
        await client.beta.memory_stores.memories.create(store.id, path=path, content=content)
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=str(agent.id))
    await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_uuid, memory_store_id=store.id
    )
    await db_session.commit()

    resolved = await get_channel_memory_store(
        db_session_factory,
        client,
        tenant_id=tenant.id,
        platform="teams",
        user_id="user-1",
        channel_id="chat-1",
        default=_DEFAULT,
    )

    assert resolved == ("daimon", store.id), "the deployment default agent's store answers"
    assert await list_memory_paths(client, store.id) == ["/a.md", "/b.md"], "paths come sorted"
    assert await get_memory_content(client, store.id, "/b.md") == "beta"
    assert await get_memory_content(client, store.id, "/missing.md") is None, "no such path"
    async with db_session_factory() as session:
        principal = await find_platform_principal(
            session, tenant_id=tenant.id, platform="teams", external_id="user-1"
        )
    assert principal is None, "browsing memory creates no principal"


async def test_channel_store_is_none_before_the_agent_has_a_store(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="teams", workspace_id="org-2")
    await db_session.commit()
    client = build_fake_anthropic(make_fake_ma_handler())
    await client.beta.agents.create(
        name="daimon",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "daimon"},
    )

    resolved = await get_channel_memory_store(
        db_session_factory,
        client,
        tenant_id=tenant.id,
        platform="teams",
        user_id="user-1",
        channel_id="chat-1",
        default=_DEFAULT,
    )

    assert resolved is None, "an agent that never ran has no memory to show"
