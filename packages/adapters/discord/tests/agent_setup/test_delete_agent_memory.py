"""delete_agent must archive the agent's memory store and clear the binding."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.discord.agent_setup.write import delete_agent
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.agent_memory_stores import (
    get_memory_store_id,
    insert_memory_store,
)
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    FakeMemoryStoreState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)

pytestmark = pytest.mark.asyncio


def _make_archive_agent_handler() -> Callable[[httpx.Request], httpx.Response]:
    """`make_fake_ma_handler` doesn't implement POST .../archive — add it here."""

    def handler(request: httpx.Request) -> httpx.Response:
        m = re.fullmatch(r"/v1/agents/(?P<id>[^/]+)/archive", request.url.path)
        if request.method != "POST" or not m:
            raise NotHandled
        now = datetime.now(UTC).isoformat()
        return httpx.Response(
            200,
            json={
                "id": m.group("id"),
                "type": "agent",
                "name": "doomed",
                "version": 2,
                "model": {"id": "claude-sonnet-4-6", "speed": "standard"},
                "system": None,
                "metadata": {},
                "mcp_servers": [],
                "tools": [],
                "skills": [],
                "created_at": now,
                "updated_at": now,
                "archived_at": now,
                "description": None,
            },
        )

    return handler


async def test_delete_agent_archives_memory_store(
    db_session, db_session_factory
) -> None:
    tenant = await make_tenant(db_session)
    mem_state = FakeMemoryStoreState()
    client = build_fake_anthropic(
        combine_handlers(
            _make_archive_agent_handler(),
            make_fake_memory_store_handler(mem_state),
            make_fake_ma_handler(),
        )
    )

    # Create an MA agent tagged for this tenant, then bind a memory store to
    # its derived UUID.
    agent = await client.beta.agents.create(
        name="doomed",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "doomed"},
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=str(agent.id))
    store = await client.beta.memory_stores.create(name="m", description="d")
    await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_uuid, memory_store_id=store.id
    )
    await db_session.commit()

    runtime = MagicMock(spec=DiscordRuntime)
    runtime.anthropic = client
    runtime.sessionmaker = db_session_factory

    await delete_agent(runtime, tenant_id=tenant.id, name="doomed")

    assert mem_state.stores[store.id]["archived_at"] is not None
    async with db_session_factory() as s:
        assert await get_memory_store_id(s, tenant_id=tenant.id, agent_id=agent_uuid) is None
