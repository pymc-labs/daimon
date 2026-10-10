"""The `memory` command, driven through the real SDK route against a fake MA memory store."""

from __future__ import annotations

import asyncio
import json

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.teams.memory import EMPTY, KEPT_INSIDE
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.agent_memory_stores import insert_memory_store
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    combine_handlers,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    CHANNEL_ID,
    DIRECT_CHAT_ID,
    ENTRA_TENANT_ID,
    TeamsApiFake,
    build_teams_runtime,
    make_channel_activity,
    make_message_activity,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


async def _anthropic(
    db_factory: async_sessionmaker[AsyncSession],
    memories: dict[str, str] | None,
    *,
    name: str = "daimon",
) -> AsyncAnthropic:
    """A fake MA with the `daimon` agent and `name`; `memories` seeds `name`'s store,
    None means no store."""
    client = build_fake_anthropic(
        combine_handlers(
            make_fake_memory_store_handler(FakeMemoryStoreState()), make_fake_ma_handler()
        )
    )
    for agent_name in dict.fromkeys(("daimon", name)):
        agent = await client.beta.agents.create(
            name=agent_name,
            model="claude-sonnet-4-6",
            metadata={"daimon_tenant": str(TENANT), "daimon_name": agent_name},
        )
    if memories is not None:
        store = await client.beta.memory_stores.create(name="m", description="d")
        for path, content in memories.items():
            await client.beta.memory_stores.memories.create(store.id, path=path, content=content)
        async with db_factory.begin() as session:
            await insert_memory_store(
                session,
                tenant_id=TENANT,
                agent_id=derive_agent_uuid(tenant_id=TENANT, ma_agent_id=str(agent.id)),
                memory_store_id=store.id,
            )
    return client


async def _replies(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    anthropic: AsyncAnthropic,
    texts: list[str],
) -> list[str]:
    """Send each text into the 1:1 chat; each reply card's JSON, in order."""
    async with running_service(
        build_teams_runtime(db_factory, anthropic=anthropic), fake
    ) as service:
        for n, text in enumerate(texts):
            await post_activity(service, make_message_activity(text=text, activity_id=f"a-{n}"))
            async with asyncio.timeout(10):
                while len(fake.activity_requests) == n:
                    await asyncio.sleep(0.01)
    return [json.dumps(r.body, ensure_ascii=False) for r in fake.activity_requests]


async def test_memory_lists_paths_then_shows_one_and_reports_a_missing_path(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    anthropic = await _anthropic(db_session_factory, {"/b.md": "beta", "/a.md": "alpha facts"})
    texts = ["memory", "memory /a.md", "memory /nope.md"]
    listing, shown, missing = await _replies(db_session_factory, teams_api_fake, anthropic, texts)

    assert "daimon's memory (2 files)" in listing and "/a.md\\n/b.md" in listing, (
        "the listing names every path, sorted"
    )
    assert "alpha facts" in shown, "a path argument shows that memory's content"
    assert "No memory file called /nope.md" in missing


async def test_memory_without_a_store_says_so(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    anthropic = await _anthropic(db_session_factory, None)
    [reply] = await _replies(db_session_factory, teams_api_fake, anthropic, ["memory"])

    assert EMPTY in reply, "an agent that never ran has nothing to show"


async def _chat_card(
    db_factory: async_sessionmaker[AsyncSession], fake: TeamsApiFake, anthropic: AsyncAnthropic
) -> str:
    """Send `memory` in a channel post; the JSON of the card answered in the 1:1 chat."""
    async with running_service(
        build_teams_runtime(db_factory, anthropic=anthropic), fake
    ) as service:
        await post_activity(service, make_channel_activity(text="memory"))
        async with asyncio.timeout(10):
            while not (chat := [r for r in fake.activity_requests if DIRECT_CHAT_ID in r.url]):
                await asyncio.sleep(0.01)
    return json.dumps(chat[0].body, ensure_ascii=False)


async def test_memory_typed_in_a_channel_shows_that_channels_agent(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    anthropic = await _anthropic(db_session_factory, {"/ops.md": "ops"}, name="helper")
    async with db_session_factory.begin() as session:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=TENANT, channel_id=CHANNEL_ID),
            tenant_id=TENANT,
            agent_name="helper",
        )
    card = await _chat_card(db_session_factory, teams_api_fake, anthropic)

    assert "helper's memory (1 files)" in card, "the channel's agent, not the 1:1 chat's"


async def test_memory_typed_in_a_limited_readers_channel_stays_out_of_the_chat(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    anthropic = await _anthropic(db_session_factory, {"/secret.md": "inside only"})
    async with db_session_factory.begin() as session:
        policy = TenantAccessPolicy(channel_rules={CHANNEL_ID: ChannelRule(readers="inside")})
        await set_access_policy(session, tenant_id=TENANT, policy=policy)
    card = await _chat_card(db_session_factory, teams_api_fake, anthropic)

    assert json.dumps(KEPT_INSIDE)[1:-1] in card, "the 1:1 chat is told why nothing is shown"
    assert "/secret.md" not in card, "no path leaves the channel"
