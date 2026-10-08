"""The chat `memory` commands' read path: which store answers in a channel, and its contents."""

from __future__ import annotations

import pytest
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.memory_view import (
    get_channel_memory_store,
    get_memory_content,
    list_memory_paths,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_memory_stores import insert_memory_store
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.thread_agent_bindings import create_binding
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


async def test_channel_store_raises_when_the_configured_agent_is_missing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="teams", workspace_id="org-3")
    await db_session.commit()

    with pytest.raises(DaimonError, match="'daimon' not found"):
        await get_channel_memory_store(
            db_session_factory,
            build_fake_anthropic(make_fake_ma_handler()),
            tenant_id=tenant.id,
            platform="teams",
            user_id="user-1",
            channel_id="chat-1",
            default=_DEFAULT,
        )


async def test_thread_store_follows_the_agent_the_thread_is_handed_to(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="teams", workspace_id="org-4")
    client = build_fake_anthropic(
        combine_handlers(
            make_fake_memory_store_handler(FakeMemoryStoreState()), make_fake_ma_handler()
        )
    )
    for name in ("daimon", "helper"):
        await client.beta.agents.create(
            name=name,
            model="claude-sonnet-4-6",
            metadata={"daimon_tenant": str(tenant.id), "daimon_name": name},
        )
    helper = [a async for a in client.beta.agents.list() if a.name == "helper"][0]
    store = await client.beta.memory_stores.create(name="m", description="d")
    await insert_memory_store(
        db_session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=str(helper.id)),
        memory_store_id=store.id,
    )
    channel, thread = "19:ops@thread.tacv2", "19:ops@thread.tacv2;messageid=1"
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="teams",
        parent_channel_id=channel,
        thread_id=thread,
        responder_ma_agent_id=str(helper.id),
        responder_name="helper",
        kind="handoff",
    )
    await db_session.commit()
    common = {
        "tenant_id": tenant.id,
        "platform": "teams",
        "user_id": "user-1",
        "channel_id": channel,
        "default": _DEFAULT,
    }

    in_thread = await get_channel_memory_store(
        db_session_factory, client, thread_id=thread, **common
    )
    in_channel = await get_channel_memory_store(db_session_factory, client, **common)

    assert in_thread == ("helper", store.id), "the thread's agent answers there"
    assert in_channel is None, "the channel's own agent has no store yet"
