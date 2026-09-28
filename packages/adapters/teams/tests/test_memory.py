"""The `memory` command, driven through the real SDK route against a fake MA memory store."""

from __future__ import annotations

import asyncio
import json

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.teams.memory import EMPTY
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.stores.agent_memory_stores import insert_memory_store
from daimon.testing.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    combine_handlers,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    ENTRA_TENANT_ID,
    TeamsApiFake,
    build_teams_runtime,
    make_message_activity,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


async def _anthropic(
    db_factory: async_sessionmaker[AsyncSession], memories: dict[str, str] | None
) -> AsyncAnthropic:
    """A fake MA with the `daimon` agent; `memories` seeds its store, None means no store."""
    client = build_fake_anthropic(
        combine_handlers(
            make_fake_memory_store_handler(FakeMemoryStoreState()), make_fake_ma_handler()
        )
    )
    agent = await client.beta.agents.create(
        name="daimon",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(TENANT), "daimon_name": "daimon"},
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


@pytest.mark.asyncio
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
    assert "No memory at /nope.md" in missing


@pytest.mark.asyncio
async def test_memory_without_a_store_says_so(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    anthropic = await _anthropic(db_session_factory, None)
    [reply] = await _replies(db_session_factory, teams_api_fake, anthropic, ["memory"])

    assert EMPTY in reply, "an agent that never ran has nothing to show"
