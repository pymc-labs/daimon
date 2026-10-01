"""Scenario (d): delete -> memory archived, on both platforms.

After `driver.delete_agent` on a tenant with an MA agent + a bound memory
store, the archive must have archived the MA-side store AND cleared the local
binding row -- identically on Discord and Slack. Deleting an agent left the
panel in the read-only rewrite, so both drivers now run the chat tool
(`tools/agents._archive_agent_impl`) with their own platform's AuthIdentity,
and it is the tool's `archive_memory_store_for_agent` call under test.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import cast

import httpx
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.agent_memory_stores import get_memory_store_id, insert_memory_store
from daimon.core.stores.domain import Platform
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    FakeMemoryStoreState,
    MARouter,
    build_fake_anthropic,
    combine_handlers,
    make_archive_agent_handler,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .drivers.protocol import PlatformDriver


def _wrap_as_marouter_handler(
    fn: Callable[[httpx.Request], httpx.Response],
) -> Callable[[httpx.Request, re.Match[str]], httpx.Response]:
    """Adapt a `combine_handlers` callable (request-only) to MARouter's
    `Handler` shape (request, match) so it can be registered as a wildcard
    route -- MARouter is the type both `PlatformDriver` methods require."""

    def handler(request: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        return fn(request)

    return handler


async def test_delete_agent_archives_memory_store(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(
        db_session,
        platform=cast(Platform, driver.param_id),
        workspace_id=f"delete-memory-{driver.param_id}",
    )
    await db_session.commit()

    mem_state = FakeMemoryStoreState()
    combined = combine_handlers(
        make_archive_agent_handler(),
        make_fake_memory_store_handler(mem_state),
        make_fake_ma_handler(),
    )
    router = MARouter()
    wrapped = _wrap_as_marouter_handler(combined)
    for method in ("GET", "POST", "PATCH", "DELETE"):
        router.add(method, r".*", wrapped)

    seed_client = build_fake_anthropic(router.dispatch)
    # `daimon_account` is what marks the agent as something this install made
    # rather than a system agent the deployment seeded; the archive path
    # refuses an unstamped agent, so the seed carries the stamp a real create
    # would have written.
    agent = await seed_client.beta.agents.create(
        name="doomed",
        model="claude-sonnet-4-6",
        metadata={
            "daimon_tenant": str(tenant.id),
            "daimon_name": "doomed",
            "daimon_account": str(derive_guild_account_uuid(tenant.id)),
        },
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=str(agent.id))
    store = await seed_client.beta.memory_stores.create(name="m", description="d")
    async with db_session_factory() as s, s.begin():
        await insert_memory_store(
            s, tenant_id=tenant.id, agent_id=agent_uuid, memory_store_id=store.id
        )

    await driver.delete_agent(
        sessionmaker=db_session_factory, router=router, tenant_id=tenant.id, name="doomed"
    )

    assert mem_state.stores[store.id]["archived_at"] is not None, (
        f"{driver.param_id}: delete_agent must archive the agent's memory store"
    )
    async with db_session_factory() as s:
        assert await get_memory_store_id(s, tenant_id=tenant.id, agent_id=agent_uuid) is None, (
            f"{driver.param_id}: delete_agent must clear the memory store binding"
        )
