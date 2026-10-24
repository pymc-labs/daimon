"""The chat `memory` commands' read path: which store answers in a channel, and its contents."""

from __future__ import annotations

import uuid

import pytest
from anthropic import AsyncAnthropic
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.memory_view import (
    ChannelAgent,
    get_channel_memory_store,
    get_memory_content,
    get_own_personal_memory_store,
    list_memory_paths,
    parse_memory_args,
    personal_memory_reply,
    resolve_channel_agent,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_memory_stores import (
    insert_channel_memory_store,
    insert_memory_store,
    insert_personal_memory_store,
)
from daimon.core.stores.identity import (
    find_platform_principal,
    get_or_create_platform_principal,
    mark_principal_foreign,
)
from daimon.core.stores.shared_channels import mark_channel_shared
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.turn_hold import RequesterOrg, TurnHold
from daimon.testing.factories import make_tenant
from mux.testing.drivers.anthropic.ma import (
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
        requester_org=RequesterOrg.HOST,
        channel_shared=False,
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
        requester_org=RequesterOrg.HOST,
        channel_shared=False,
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
            requester_org=RequesterOrg.HOST,
            channel_shared=False,
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
        "requester_org": RequesterOrg.HOST,
        "channel_shared": False,
    }

    in_thread = await get_channel_memory_store(
        db_session_factory, client, thread_id=thread, **common
    )
    in_channel = await get_channel_memory_store(db_session_factory, client, **common)

    assert in_thread == ("helper", store.id), "the thread's agent answers there"
    assert in_channel is None, "the channel's own agent has no store yet"


async def _agent_with_stores(
    db_session: AsyncSession, tenant_id: uuid.UUID, *, channel: str | None = "C-SYN-1"
) -> tuple[AsyncAnthropic, str, str | None, str | None]:
    """A fake MA with the default agent, its agent-wide store and a channel's store."""
    client = build_fake_anthropic(
        combine_handlers(
            make_fake_memory_store_handler(FakeMemoryStoreState()), make_fake_ma_handler()
        )
    )
    agent = await client.beta.agents.create(
        name="daimon",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "daimon"},
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id))
    wide = await client.beta.memory_stores.create(name="wide", description="d")
    await insert_memory_store(
        db_session, tenant_id=tenant_id, agent_id=agent_uuid, memory_store_id=wide.id
    )
    own: str | None = None
    if channel is not None:
        store = await client.beta.memory_stores.create(name="channel", description="d")
        own = await insert_channel_memory_store(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            platform="slack",
            channel_id=channel,
            memory_store_id=store.id,
        )
    return client, wide.id, own, str(agent_uuid)


async def _show(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncAnthropic,
    tenant_id: uuid.UUID,
    *,
    channel_id: str = "C-SYN-1",
    user_id: str = "U-SYN-1",
) -> tuple[str, str] | None:
    return await get_channel_memory_store(
        session_factory,
        client,
        tenant_id=tenant_id,
        platform="slack",
        user_id=user_id,
        channel_id=channel_id,
        default=_DEFAULT,
        requester_org=RequesterOrg.HOST,
        channel_shared=False,
    )


async def test_a_shared_channel_shows_its_own_store_and_others_show_the_agent_wide_one(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, wide, own, _ = await _agent_with_stores(db_session, tenant.id)
    await mark_channel_shared(
        db_session, tenant_id=tenant.id, platform="slack", channel_id="C-SYN-1", source="platform"
    )
    await db_session.commit()

    shared = await _show(db_session_factory, client, tenant.id)
    elsewhere = await _show(db_session_factory, client, tenant.id, channel_id="C-SYN-9")

    assert shared == ("daimon", own), "a held place shows its channel's store, never agent-wide"
    assert elsewhere == ("daimon", wide), "an unheld place never shows a channel's store"


async def test_a_requester_from_another_organisation_sees_the_channel_store_anywhere(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, _, own, _ = await _agent_with_stores(db_session, tenant.id)
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id="U-SYN-2"
    )
    await mark_principal_foreign(db_session, principal_id=principal.id, home_id="HOME-SYN")
    await db_session.commit()

    assert await _show(db_session_factory, client, tenant.id, user_id="U-SYN-2") == ("daimon", own)


async def test_a_held_place_with_no_channel_store_yet_shows_nothing_not_the_agent_wide_one(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, _, _, _ = await _agent_with_stores(db_session, tenant.id, channel=None)
    await mark_channel_shared(
        db_session, tenant_id=tenant.id, platform="slack", channel_id="C-SYN-1", source="platform"
    )
    await db_session.commit()

    assert await _show(db_session_factory, client, tenant.id) is None


async def test_personal_memory_is_not_used_in_a_held_place(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, _, _, agent_uuid = await _agent_with_stores(db_session, tenant.id)
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id="U-SYN-1"
    )
    await insert_personal_memory_store(
        db_session,
        tenant_id=tenant.id,
        agent_id=uuid.UUID(agent_uuid),
        account_id=principal.account_id,
        memory_store_id="memstore_personal",
    )
    await mark_channel_shared(
        db_session, tenant_id=tenant.id, platform="slack", channel_id="C-SYN-1", source="platform"
    )
    await db_session.commit()

    async def resolve(channel_id: str) -> ChannelAgent:
        agent = await resolve_channel_agent(
            db_session_factory,
            client,
            tenant_id=tenant.id,
            platform="slack",
            user_id="U-SYN-1",
            channel_id=channel_id,
            default=_DEFAULT,
            requester_org=RequesterOrg.HOST,
            channel_shared=False,
        )
        assert agent is not None
        return agent

    held, unheld = await resolve("C-SYN-1"), await resolve("C-SYN-9")

    assert held.hold == TurnHold("slack", "C-SYN-1", client=False)
    assert unheld.hold is None
    assert await get_own_personal_memory_store(db_session_factory, held) is None
    assert await get_own_personal_memory_store(db_session_factory, unheld) == "memstore_personal"
    for text in ("mine", "mine /a.md", "forget-mine", "forget-mine abc123"):
        reply = await personal_memory_reply(
            client,
            agent_name="daimon",
            store_id=await get_own_personal_memory_store(db_session_factory, held),
            request=parse_memory_args(text),
            command="/memory",
            held=True,
        )
        assert reply.title == "Personal memory isn't used in this channel."
        assert reply.paths == () and reply.content is None
        assert reply.hint is not None and "/memory" in reply.hint


@pytest.mark.parametrize(
    ("requester_org", "channel_shared"),
    [
        (RequesterOrg.UNKNOWN, False),
        (RequesterOrg.UNKNOWN, None),
        (RequesterOrg.OTHER, False),
        (RequesterOrg.HOST, True),
    ],
    ids=["unknown", "unknown-no-signal", "other", "host-in-live-shared-channel"],
)
async def test_an_asker_not_proven_a_member_of_an_unshared_channel_never_reads_agent_wide_memory(
    requester_org: RequesterOrg,
    channel_shared: bool | None,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With nothing recorded yet (no marker, no registry row), the live facts alone
    decide: only HOST in a channel not live-signalled shared reads agent-wide memory."""
    tenant = await make_tenant(db_session)
    client, wide, own, _ = await _agent_with_stores(db_session, tenant.id)
    await db_session.commit()

    async def show(org: RequesterOrg, shared: bool | None) -> tuple[str, str] | None:
        return await get_channel_memory_store(
            db_session_factory,
            client,
            tenant_id=tenant.id,
            platform="slack",
            user_id="U-SYN-1",
            channel_id="C-SYN-1",
            default=_DEFAULT,
            requester_org=org,
            channel_shared=shared,
        )

    assert await show(requester_org, channel_shared) == ("daimon", own)
    assert await show(RequesterOrg.HOST, False) == ("daimon", wide)
    assert await show(RequesterOrg.HOST, None) == ("daimon", wide), "no signal, nothing recorded"
    async with db_session_factory() as session:
        principal = await find_platform_principal(
            session, tenant_id=tenant.id, platform="slack", external_id="U-SYN-1"
        )
        assert principal is None, "nothing is recorded about an asker from here"
