"""daimon.core.agent_lifecycle: credential-free forks and best-effort delete archival."""

from __future__ import annotations

import uuid

import httpx
from cryptography.fernet import Fernet
from daimon.core.agent_lifecycle import (
    archive_memory_store_best_effort,
    strip_credentialed_mcp_servers,
)
from daimon.core.agent_mcp_credentials import save_agent_mcp_credential
from daimon.core.github_credentials import build_multifernet
from daimon.core.stores.agent_memory_stores import get_memory_store_id, insert_memory_store
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    make_fake_memory_store_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_fork_drops_every_server_backed_by_a_stored_token(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A fork must never become a second holder of the source's connector tokens;
    the servers those tokens unlock are left off rather than mounted broken."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    source = uuid.uuid4()
    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
        tenant_id=tenant.id,
        agent_id=source,
        mcp_server_url="https://crm.example.com/mcp",
        plaintext_token="tok_client_a",
    )
    servers: list[dict[str, object]] = [
        {"type": "url", "name": "crm", "url": "https://crm.example.com/mcp/"},
        {"type": "url", "name": "docs", "url": "https://docs.example.com/mcp"},
    ]
    tools: list[dict[str, object]] = [
        {"type": "agent_toolset_20260401"},
        {"type": "mcp_toolset", "mcp_server_name": "crm"},
        {"type": "mcp_toolset", "mcp_server_name": "docs"},
    ]

    kept_servers, kept_tools = await strip_credentialed_mcp_servers(
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        source_agent_uuid=source,
        mcp_servers=servers,
        tools=tools,
    )

    assert kept_servers == [servers[1]], "the token-backed server must not reach the fork"
    assert kept_tools == [tools[0], tools[2]], "its toolset goes with it"


async def test_fork_keeps_everything_when_the_source_holds_no_tokens(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    servers: list[dict[str, object]] = [{"type": "url", "name": "docs", "url": "https://d/mcp"}]
    tools: list[dict[str, object]] = [{"type": "mcp_toolset", "mcp_server_name": "docs"}]

    assert await strip_credentialed_mcp_servers(
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        source_agent_uuid=uuid.uuid4(),
        mcp_servers=servers,
        tools=tools,
    ) == (servers, tools)


async def test_archive_best_effort_happy_path(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id = uuid.uuid4()
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))

    store_id = "memstore_archive_happy"
    async with db_session_factory() as s, s.begin():
        await insert_memory_store(
            s, tenant_id=tenant.id, agent_id=agent_id, memory_store_id=store_id
        )
    state.stores[store_id] = {"archived_at": None}

    await archive_memory_store_best_effort(
        anthropic=client,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        agent_id=agent_id,
        log_context={"tenant_id": str(tenant.id), "agent_name": "doomed"},
    )

    assert state.stores[store_id]["archived_at"] is not None
    async with db_session_factory() as s:
        assert await get_memory_store_id(s, tenant_id=tenant.id, agent_id=agent_id) is None


async def test_archive_best_effort_degrades_on_api_error(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A transient MA outage on the archive call must not raise; the DB
    binding remains untouched (inert — nothing re-reads an archived agent)."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id = uuid.uuid4()

    store_id = "memstore_archive_degrade"
    async with db_session_factory() as s, s.begin():
        await insert_memory_store(
            s, tenant_id=tenant.id, agent_id=agent_id, memory_store_id=store_id
        )

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
        )

    client = build_fake_anthropic(failing_handler)

    # Must not raise despite the 500 from the store archive.
    await archive_memory_store_best_effort(
        anthropic=client,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        agent_id=agent_id,
        log_context={"tenant_id": str(tenant.id), "agent_name": "doomed"},
    )

    async with db_session_factory() as s:
        assert await get_memory_store_id(s, tenant_id=tenant.id, agent_id=agent_id) == store_id
