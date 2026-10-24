"""daimon.core.agent_lifecycle: credential-free forks and best-effort delete archival."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.core import agent_lifecycle
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.agent_lifecycle import (
    archive_memory_store_best_effort,
    forget_archived_agent,
    strip_credentialed_mcp_servers,
)
from daimon.core.agent_mcp_credentials import save_agent_mcp_credential
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_READER_OF,
    MA_METADATA_KEY_READER_SOURCE,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores.access_policy import (
    AccessPolicyUnreadable,
    PolicyBusyError,
    load_access_policy,
    set_access_policy,
)
from daimon.core.stores.agent_memory_stores import get_memory_store_id, insert_memory_store
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import (
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


async def _seed_archived(
    session: AsyncSession, tenant_id: uuid.UUID, rules: dict[str, AgentRule]
) -> ChannelScopeRef:
    scope = ChannelScopeRef(tenant_id=tenant_id, channel_id="c1")
    await set_fields(session, scope=scope, tenant_id=tenant_id, agent_name="doomed")
    await set_access_policy(
        session, tenant_id=tenant_id, policy=TenantAccessPolicy(agent_rules=rules)
    )
    await session.commit()
    return scope


async def test_forget_archived_agent_drops_its_rules_and_scope_rows(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An archived agent's rule must not outlive it: a later agent taking
    the name, such as a channel copy, would inherit it. A report reader also
    answers to its source's names; those rules are the source's and stay."""
    tenant = await make_tenant(db_session)
    rules = {
        "doomed": AgentRule(runs_in=("c1",)),
        "doomed-ma": AgentRule(runs_in=("c1",)),
        "source": AgentRule(runs_in=("c2",)),
        "bystander": AgentRule(runs_in=("c3",)),
    }
    scope = await _seed_archived(db_session, tenant.id, rules)
    ma_scope = ChannelScopeRef(tenant_id=tenant.id, channel_id="c2")
    await set_fields(db_session, scope=ma_scope, tenant_id=tenant.id, agent_name="doomed-ma")
    await db_session.commit()
    agent = ma_agent(
        id="ag_doomed",
        name="doomed-ma",
        tenant_id=tenant.id,
        metadata={
            MA_METADATA_KEY_NAME: "doomed",
            MA_METADATA_KEY_READER_OF: "x",
            MA_METADATA_KEY_READER_SOURCE: "source",
        },
    )

    dropped = await forget_archived_agent(
        db_session_factory, tenant_id=tenant.id, name="doomed", agent=agent
    )

    assert dropped == ("doomed", "doomed-ma")
    async with db_session_factory() as s:
        policy = await load_access_policy(s, tenant_id=tenant.id)
        assert policy.agent_rules == {
            "source": AgentRule(runs_in=("c2",)),
            "bystander": AgentRule(runs_in=("c3",)),
        }, "only the archived agent's own names lose their rule"
        assert await get_scope(s, scope=scope) is None, "scope rows naming it are cleared too"
        assert await get_scope(s, scope=ma_scope) is None, "under each of its own names"


@asynccontextmanager
async def _busy(*_args: object, **_kwargs: object) -> AsyncIterator[AsyncSession]:
    raise PolicyBusyError()
    yield  # pyright: ignore[reportUnreachable]


@pytest.mark.parametrize("failure", ["unreadable", "busy"])
async def test_forget_archived_agent_still_clears_scope_rows_when_the_policy_is_out_of_reach(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """The agent is archived already; a policy that can't be read or locked
    must not leave channels resolving to it. The rule is left for an operator."""
    tenant = await make_tenant(db_session)
    scope = await _seed_archived(db_session, tenant.id, {"doomed": AgentRule(runs_in=("c1",))})

    async def unreadable(_session: AsyncSession, *, tenant_id: uuid.UUID) -> TenantAccessPolicy:
        raise AccessPolicyUnreadable(tenant_id=tenant_id)

    if failure == "unreadable":
        monkeypatch.setattr(agent_lifecycle, "load_access_policy", unreadable)
    else:
        monkeypatch.setattr(agent_lifecycle, "policy_write_transaction", _busy)
    agent = ma_agent(id="ag_doomed", name="doomed", tenant_id=tenant.id)

    dropped = await forget_archived_agent(
        db_session_factory, tenant_id=tenant.id, name="doomed", agent=agent
    )

    assert dropped == ()
    async with db_session_factory() as s:
        assert await get_scope(s, scope=scope) is None
        assert "doomed" in (await load_access_policy(s, tenant_id=tenant.id)).agent_rules
