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
    """Send `memory` in a channel post; the JSON of the card only its sender sees there."""
    async with running_service(
        build_teams_runtime(db_factory, anthropic=anthropic), fake
    ) as service:
        await post_activity(service, make_channel_activity(text="memory"))
        async with asyncio.timeout(10):
            while not (chat := fake.private_requests):
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


async def test_memory_typed_in_a_limited_readers_channel_is_shown_inside_it(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    anthropic = await _anthropic(db_session_factory, {"/secret.md": "inside only"})
    async with db_session_factory.begin() as session:
        policy = TenantAccessPolicy(channel_rules={CHANNEL_ID: ChannelRule(readers="inside")})
        await set_access_policy(session, tenant_id=TENANT, policy=policy)
    card = await _chat_card(db_session_factory, teams_api_fake, anthropic)

    assert "/secret.md" in card, "answered privately in the channel, nothing leaves it"
    assert json.dumps(KEPT_INSIDE)[1:-1] not in card


async def _seed_personal(
    db_factory: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    aad_object_id: str,
    files: dict[str, str],
) -> str:
    """Bind a personal store for the Teams user, as their first chat session would."""
    from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
    from daimon.core.stores.agent_memory_stores import insert_personal_memory_store
    from daimon.core.stores.identity import get_or_create_platform_principal

    agent = await find_agent_by_daimon_tag(anthropic, tenant_id=TENANT, name="daimon")
    assert agent is not None
    store = await anthropic.beta.memory_stores.create(name=f"personal {aad_object_id}")
    for path, content in files.items():
        await anthropic.beta.memory_stores.memories.create(store.id, path=path, content=content)
    async with db_factory.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=TENANT, platform="teams", external_id=aad_object_id
        )
        await insert_personal_memory_store(
            session,
            tenant_id=TENANT,
            agent_id=derive_agent_uuid(tenant_id=TENANT, ma_agent_id=str(agent.id)),
            account_id=principal.account_id,
            memory_store_id=store.id,
        )
    return store.id


async def test_memory_mine_shows_and_clears_only_the_senders_own_after_confirming(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    from daimon.core.memory_view import MemoryRequest, personal_memory_reply

    from .conftest import AAD_OBJECT_ID, OTHER_AAD_OBJECT_ID

    anthropic = await _anthropic(db_session_factory, {"/team.md": "fridays"})
    mine = await _seed_personal(
        db_session_factory, anthropic, aad_object_id=AAD_OBJECT_ID, files={"/style.md": "tabs"}
    )
    theirs = await _seed_personal(
        db_session_factory,
        anthropic,
        aad_object_id=OTHER_AAD_OBJECT_ID,
        files={"/secret-pref.md": "someone else's"},
    )
    # The code the preview offers, from the same core reply the command renders.
    preview = await personal_memory_reply(
        anthropic,
        agent_name="daimon",
        store_id=mine,
        request=MemoryRequest("forget_ask"),
        command="memory",
    )
    assert preview.hint is not None
    code = preview.hint.split()[-1]
    texts = ["memory mine", "memory forget-mine", f"memory forget-mine {code}", "memory"]
    listing, asked, done, shared = await _replies(
        db_session_factory, teams_api_fake, anthropic, texts
    )

    assert "/style.md" in listing and "/secret-pref.md" not in listing
    assert code in asked, "the first step only shows the code"
    assert "Deleted 1 file " in done
    remaining = [
        item.path
        async for item in await anthropic.beta.memory_stores.memories.list(mine, path_prefix="/")
    ]
    assert remaining == []
    kept = [
        item.path
        async for item in await anthropic.beta.memory_stores.memories.list(theirs, path_prefix="/")
    ]
    assert kept == ["/secret-pref.md"], "another person's store is never touched"
    assert "/team.md" in shared, "the shared memory is untouched"


async def test_a_long_forget_preview_shows_every_file_before_its_code(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """The code deletes exactly the listed files, so a preview longer than one
    card goes over several: every path is delivered, the code only on the last."""
    from daimon.core.memory_view import MemoryRequest, personal_memory_reply

    from .conftest import AAD_OBJECT_ID

    anthropic = await _anthropic(db_session_factory, {"/team.md": "fridays"})
    files = {f"/notes/{n:02d}-{'x' * 90}.md": "pref" for n in range(50)}
    mine = await _seed_personal(
        db_session_factory, anthropic, aad_object_id=AAD_OBJECT_ID, files=files
    )
    preview = await personal_memory_reply(
        anthropic,
        agent_name="daimon",
        store_id=mine,
        request=MemoryRequest("forget_ask"),
        command="memory",
    )
    assert preview.hint is not None
    code = preview.hint.split()[-1]

    async with running_service(
        build_teams_runtime(db_session_factory, anthropic=anthropic), teams_api_fake
    ) as service:
        await post_activity(service, make_message_activity(text="memory forget-mine"))
        async with asyncio.timeout(10):
            while not any(code in json.dumps(r.body) for r in teams_api_fake.activity_requests):
                await asyncio.sleep(0.01)
    cards = [json.dumps(r.body, ensure_ascii=False) for r in teams_api_fake.activity_requests]

    assert len(cards) > 1, "more than one card's worth of paths"
    assert all(len(card) < 8000 for card in cards)
    for path in files:
        assert any(path in card for card in cards), f"{path} was not shown"
    assert code in cards[-1] and not any(code in card for card in cards[:-1])
    assert "truncated" not in "".join(cards)
