"""Smoke tests for the daimon.testing.factories additions (05-03).

Each new make_* factory is asserted to persist a row and return the Pydantic
row type the code under test sees (not ORM), backed by the real store helper.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from daimon.core.allowance_limits import monthly_limit
from daimon.core.stores import mcp_tokens
from daimon.core.stores.agent_memory_stores import get_memory_store_id
from daimon.core.stores.domain import (
    AgentMemoryStoreRow,
    McpTokenRow,
    RoutineRow,
    SlackUserTokenRow,
    TenantLedgerRow,
    ThreadSessionRow,
    UsageEventRow,
)
from daimon.core.stores.routines import get_routine
from daimon.core.stores.slack_user_tokens import get_slack_user_token
from daimon.core.stores.tenant_ledger import get_balance
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.testing.factories import (
    make_account,
    make_agent_memory_store,
    make_ledger_entry,
    make_mcp_token,
    make_monthly_limit,
    make_routine,
    make_slack_user_token,
    make_tenant,
    make_tenant_config,
    make_thread_session,
    make_usage_event,
)
from sqlalchemy.ext.asyncio import AsyncSession


async def test_make_routine_persists_and_returns_routine_row(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    routine = await make_routine(db_session, tenant=tenant, agent_id="agent-42")

    assert isinstance(routine, RoutineRow), "make_routine should return a Pydantic RoutineRow"
    assert routine.agent_id == "agent-42", "make_routine should honor the passed agent_id"

    persisted = await get_routine(db_session, routine.id, tenant_id=tenant.id)
    assert persisted is not None, "make_routine's row should be queryable back from the store"
    assert persisted.id == routine.id, "queried row should match the created routine"


async def test_make_tenant_config_upserts_without_uniqueness_error(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)

    first = await make_tenant_config(db_session, tenant=tenant, agent_name="agent-a")
    second = await make_tenant_config(db_session, tenant=tenant, agent_name="agent-b")

    assert isinstance(first, object), "sanity: first call should not raise"
    assert second.agent_name == "agent-b", (
        "calling make_tenant_config twice for the same tenant should upsert, not conflict"
    )
    assert second.tenant_id == tenant.id, "returned row should be scoped to the passed tenant"


async def test_make_usage_event_persists_and_returns_usage_event_row(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    event = await make_usage_event(db_session, tenant=tenant, input_tokens=123)

    assert isinstance(event, UsageEventRow), "make_usage_event should return a Pydantic row"
    assert event.input_tokens == 123, "make_usage_event should honor passed token counts"
    assert event.tenant_id == tenant.id


async def test_make_usage_event_with_null_platform_user_id(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    event = await make_usage_event(db_session, tenant=tenant, platform_user_id=None)

    assert event.platform_user_id is None, (
        "make_usage_event(platform_user_id=None) should persist a NULL platform_user_id row"
    )


async def test_make_ledger_entry_persists_and_returns_tenant_ledger_row(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    entry = await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("15"))

    assert isinstance(entry, TenantLedgerRow), "make_ledger_entry should return a Pydantic row"
    balance = await get_balance(db_session, tenant_id=tenant.id)
    assert balance == Decimal("15"), "ledger balance should reflect the inserted entry"


async def test_make_slack_user_token_persists_and_returns_slack_user_token_row(
    db_session: AsyncSession,
) -> None:
    token = await make_slack_user_token(db_session, slack_user_id="U999")

    assert isinstance(token, SlackUserTokenRow), (
        "make_slack_user_token should return a Pydantic row"
    )
    persisted = await get_slack_user_token(db_session, team_id=token.team_id, slack_user_id="U999")
    assert persisted is not None, "make_slack_user_token's row should be queryable back"


async def test_make_monthly_limit_default_and_person(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)

    default = await make_monthly_limit(db_session, tenant=tenant, amount=Decimal("20"))
    assert (default.subject_kind, default.subject_id) == ("group", "everyone"), (
        "user_id=None should create the workspace default"
    )

    own = await make_monthly_limit(db_session, tenant=tenant, user_id="U1", amount=Decimal("5"))
    assert (own.subject_kind, own.subject_id) == ("person", "U1"), (
        "passing user_id should create that person's own limit"
    )

    limit = await monthly_limit(
        db_session, tenant_id=tenant.id, platform_user_id="U1", now=datetime.now(UTC)
    )
    assert limit == Decimal("5"), "a person's own limit takes priority over the default"


async def test_make_agent_memory_store_persists_and_returns_row(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    binding = await make_agent_memory_store(db_session, tenant=tenant, agent_id=agent_id)

    assert isinstance(binding, AgentMemoryStoreRow), (
        "make_agent_memory_store should return a Pydantic row"
    )
    persisted_id = await get_memory_store_id(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert persisted_id == binding.memory_store_id, "binding should be queryable back by PK"


async def test_make_thread_session_persists_and_returns_row(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)

    thread_session = await make_thread_session(db_session, tenant=tenant, account=account)

    assert isinstance(thread_session, ThreadSessionRow), (
        "make_thread_session should return a Pydantic row"
    )
    persisted = await get_live_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform=thread_session.platform,
        thread_id=thread_session.thread_id,
        account_id=account.id,
    )
    assert persisted is not None, "make_thread_session's row should be queryable back"


async def test_make_mcp_token_persists_and_returns_row(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)

    token = await make_mcp_token(db_session, tenant=tenant, account=account)

    assert isinstance(token, McpTokenRow), "make_mcp_token should return a Pydantic row"
    persisted = await mcp_tokens.get_mcp_token(db_session, jti=token.jti)
    assert persisted is not None, "make_mcp_token's row should be queryable back"
