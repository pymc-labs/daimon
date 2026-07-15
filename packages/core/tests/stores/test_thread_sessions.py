"""DB-backed unit tests for `daimon.core.stores.thread_sessions`.

Covers create + get_live, newest-row-wins ordering, dead-row exclusion,
watermark update, restart-resume (SC-1 store half + SC-3), account_id
filtering (Phase 88 SCOPING §4/§6 security guard), and NULL-never-matches
proof against real Postgres.
Each test inlines its `create_thread_session(...)` call per guideline:testing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from daimon.core._models import ThreadSession
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    get_live_thread_session,
    mark_dead,
    update_watermark,
)
from daimon.testing.factories import make_tenant
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def tenant_id(db_session: AsyncSession) -> uuid.UUID:
    tenant = await make_tenant(db_session)
    return tenant.id


async def test_create_then_get_live_returns_row(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    account_id = uuid.uuid4()
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="111",
        account_id=account_id,
        ma_session_id="sess_a",
    )
    assert isinstance(row, ThreadSessionRow), "store must return Pydantic, not ORM"

    fetched = await get_live_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="111",
        account_id=account_id,
    )
    assert fetched is not None, "get_live_thread_session must return a row after create"
    assert fetched.ma_session_id == "sess_a", "fetched row must carry the created ma_session_id"
    assert fetched.status == "live", "created row must default to status='live'"


async def test_get_live_newest_row_wins(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    account_id = uuid.uuid4()
    await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="111",
        account_id=account_id,
        ma_session_id="sess_old",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="111",
        account_id=account_id,
        ma_session_id="sess_new",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    fetched = await get_live_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="111",
        account_id=account_id,
    )
    assert fetched is not None, "get_live_thread_session must return a row when two exist"
    assert fetched.ma_session_id == "sess_new", (
        "newest-row-wins: get_live must return the row with the latest created_at"
    )


async def test_mark_dead_excludes_from_live_but_keeps_row(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    account_id = uuid.uuid4()
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="222",
        account_id=account_id,
        ma_session_id="sess_b",
    )
    row_id = row.id

    await mark_dead(db_session, id=row_id)

    live = await get_live_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="222",
        account_id=account_id,
    )
    assert live is None, "mark_dead must exclude the row from live lookup"

    # Verify the row is retained in the table as audit (ORM escape hatch per guideline:testing)
    orm = (
        await db_session.execute(select(ThreadSession).where(ThreadSession.id == row_id))
    ).scalar_one_or_none()
    assert orm is not None, "mark_dead must retain the row in the table (audit trail)"
    assert orm.status == "dead", "mark_dead must set status='dead' on the row"


async def test_update_watermark_persists(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    account_id = uuid.uuid4()
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="333",
        account_id=account_id,
        ma_session_id="sess_c",
    )
    assert row.watermark_message_id is None, "freshly created row must have watermark=None"

    await update_watermark(db_session, id=row.id, watermark_message_id="999")

    fetched = await get_live_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="333",
        account_id=account_id,
    )
    assert fetched is not None, "row must still be live after watermark update"
    assert fetched.watermark_message_id == "999", (
        "update_watermark must persist the supplied watermark_message_id"
    )


async def test_restart_resume_reads_same_live_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> None:
    """Simulates a deploy restart: create in one session, read in a fresh session.

    db_session_factory produces sessions sharing the same per-test connection
    so commits are visible without a DB network round-trip, accurately
    simulating SC-3 (mapping survives session lifecycle / restart).
    """
    account_id = uuid.uuid4()
    # Create in the shared test session and commit so it is visible to a fresh session.
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="444",
        account_id=account_id,
        ma_session_id="sess_restart",
    )
    await db_session.commit()

    # Open a fresh session (simulates process restart re-reading from DB).
    async with db_session_factory() as fresh_session:
        fetched = await get_live_thread_session(
            fresh_session,
            tenant_id=tenant_id,
            platform="discord",
            thread_id="444",
            account_id=account_id,
        )
    assert fetched is not None, "mapping must survive a fresh session (restart resume)"
    assert fetched.ma_session_id == "sess_restart", (
        "fresh session must return the same ma_session_id (SC-3 restart survival)"
    )
    assert fetched.id == row.id, "fresh session must return the same row id"


async def test_get_live_returns_none_when_no_mapping(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    result = await get_live_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="nonexistent",
        account_id=uuid.uuid4(),
    )
    assert result is None, "get_live_thread_session must return None when no mapping exists"


async def test_get_live_thread_session_filters_by_account_id(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """Caller A's lookup returns A's row; caller B's row is never returned to A."""
    account_a = uuid.uuid4()
    account_b = uuid.uuid4()

    await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="filter-test",
        account_id=account_a,
        ma_session_id="sess_a",
    )
    await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="filter-test",
        account_id=account_b,
        ma_session_id="sess_b",
    )

    fetched_a = await get_live_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="filter-test",
        account_id=account_a,
    )
    assert fetched_a is not None, "caller A must get a result when a row exists for their account"
    assert fetched_a.ma_session_id == "sess_a", "caller A must receive their own session, not B's"

    fetched_b = await get_live_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="filter-test",
        account_id=account_b,
    )
    assert fetched_b is not None, "caller B must get a result when a row exists for their account"
    assert fetched_b.ma_session_id == "sess_b", "caller B must receive their own session, not A's"


async def test_get_live_thread_session_null_account_row_never_matches_live_caller(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """Security guard: a NULL account_id row must NEVER be returned for any non-null caller.

    This covers the pre-migration frozen-row scenario: existing rows have
    account_id=NULL and must fall through to a cold create for the first turn
    after deploy. The predicate must be a plain equality (no OR IS NULL).

    Two assertions:
    1. get_live_thread_session returns None for the real caller uuid.
    2. The NULL row physically exists in the table (proves it was not deleted).
    """
    # Insert a NULL-account row directly via ORM (bypasses store to set NULL explicitly)
    null_row = ThreadSession(
        tenant_id=tenant_id,
        platform="discord",
        thread_id="null-account-test",
        account_id=None,
        ma_session_id="sess_frozen",
    )
    db_session.add(null_row)
    await db_session.flush()
    await db_session.refresh(null_row)

    live_caller_id = uuid.uuid4()
    result = await get_live_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="null-account-test",
        account_id=live_caller_id,
    )
    assert result is None, (
        "security guard: NULL-account row must never be returned for a non-null caller "
        "(no OR IS NULL in the WHERE clause)"
    )

    # DB-shape check: the NULL row must still physically exist (it was not deleted)
    orm_null = (
        await db_session.execute(select(ThreadSession).where(ThreadSession.account_id.is_(None)))
    ).scalar_one_or_none()
    assert orm_null is not None, (
        "NULL-account row must still exist in the table after the failed lookup "
        "(frozen rows are kept as inert audit history)"
    )
    assert orm_null.ma_session_id == "sess_frozen", (
        "the NULL-account row's ma_session_id must be unchanged"
    )


async def test_create_thread_session_persists_account_id(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """account_id passed to create_thread_session must round-trip through model_validate."""
    account_id = uuid.uuid4()
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="persist-test",
        account_id=account_id,
        ma_session_id="sess_persist",
    )
    assert row.account_id == account_id, (
        "create_thread_session must persist the caller's account_id on the new row"
    )

    # Confirm it round-trips through the DB (not just the in-memory ORM object)
    fetched = await get_live_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="persist-test",
        account_id=account_id,
    )
    assert fetched is not None, "newly created row must be returned by get_live_thread_session"
    assert fetched.account_id == account_id, (
        "account_id must survive the DB round-trip via get_live_thread_session"
    )
