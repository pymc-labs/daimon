"""DB-backed tests for the `agent_name` column on routines.

Covers create/update kwarg plumbing and Pydantic round-trip. Each test inlines
its `create_routine(...)` call (per `guideline:testing`).
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from daimon.core.stores.routines import (
    create_routine,
    get_routine,
    update_routine,
    update_routine_agent_id,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture
async def tenant_id(db_session: AsyncSession) -> uuid.UUID:
    tenant = await make_tenant(db_session)
    return tenant.id


async def test_create_routine_persists_agent_name(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id="u1",
        agent_id="agt_live",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="hi",
    )
    assert row.agent_name == "daimon", (
        "create_routine should persist agent_name on the returned row"
    )

    fetched = await get_routine(db_session, row.id)
    assert fetched is not None, "freshly created routine must be fetchable"
    assert fetched.agent_name == "daimon", "agent_name should round-trip via get_routine"


async def test_update_routine_writes_agent_name(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """update_routine should overwrite agent_name to a new value (e.g. rename path)."""
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agt_x",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="hi",
    )
    assert row.agent_name == "daimon", "precondition: row was created with agent_name='daimon'"

    updated = await update_routine(db_session, row.id, agent_name="other")
    assert updated is not None, "update_routine should return the row for an existing id"
    assert updated.agent_name == "other", "update_routine should rewrite agent_name when provided"


async def test_update_routine_omits_agent_name_when_none(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agt_x",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="hi",
    )
    assert row.agent_name == "daimon", "precondition: row was created with agent_name='daimon'"

    # Update only cron_expr; agent_name=None should mean "no change" (PATCH semantics).
    updated = await update_routine(db_session, row.id, cron_expr="*/5 * * * *")
    assert updated is not None
    assert updated.cron_expr == "*/5 * * * *", "cron_expr should be updated"
    assert updated.agent_name == "daimon", (
        "agent_name=None (default) should leave the prior value intact"
    )


async def test_update_routine_agent_id_only_changes_agent_id(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """Dedicated helper for the resolver self-heal write.

    Must only touch ``agent_id`` — everything else (cron_expr, timezone,
    trigger_message, enabled, agent_name, next_fire_at) stays put.
    """
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id="u1",
        agent_id="ag_old",
        agent_name="daimon",
        cron_expr="*/15 * * * *",
        timezone_="UTC",
        trigger_message="ping",
    )
    await db_session.flush()

    changed = await update_routine_agent_id(db_session, row.id, "ag_new")
    assert changed is True, "update_routine_agent_id should report a row was updated"

    fetched = await get_routine(db_session, row.id)
    assert fetched is not None
    assert fetched.agent_id == "ag_new", "agent_id should be rewritten to the new id"
    assert fetched.agent_name == "daimon", "agent_name must be untouched"
    assert fetched.cron_expr == "*/15 * * * *", "cron_expr must be untouched"
    assert fetched.timezone == "UTC", "timezone must be untouched"
    assert fetched.trigger_message == "ping", "trigger_message must be untouched"
    assert fetched.enabled is True, "enabled must be untouched"


async def test_update_routine_agent_id_returns_false_for_missing_row(
    db_session: AsyncSession,
) -> None:
    import uuid as _uuid

    changed = await update_routine_agent_id(db_session, _uuid.uuid4(), "ag_x")
    assert changed is False, "missing routine_id should return False, not raise"
