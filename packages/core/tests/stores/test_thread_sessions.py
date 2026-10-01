"""DB-backed unit tests for `daimon.core.stores.thread_sessions`.

Covers create + get_live, newest-row-wins ordering, dead-row exclusion,
watermark update, restart-resume (SC-1 store half + SC-3), account_id
filtering (SCOPING §4/§6 security guard), and NULL-never-matches
proof against real Postgres.
Each test inlines its `create_thread_session(...)` call per guideline:testing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from daimon.core._models import ThreadSession
from daimon.core.session_snapshot import SessionSnapshot
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.stores.thread_session_lineage import (
    clear_fresh_start,
    get_lineage,
    mark_retired,
    mark_superseded,
    request_fresh_start,
)
from daimon.core.stores.thread_sessions import (
    clear_active_turn,
    clear_active_turn_if_message_id,
    create_thread_session,
    get_latest_thread_session,
    get_live_thread_session,
    get_thread_session_by_id,
    list_orphaned_turns,
    mark_dead,
    mark_turn_active,
    record_snapshot,
    set_pending_unsaved_work,
    update_mutable_fingerprint,
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


async def test_get_latest_thread_session_returns_newest_row_across_accounts(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """Unlike get_live_thread_session, this ignores account_id entirely."""
    account_a = uuid.uuid4()
    account_b = uuid.uuid4()
    await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="latest-test",
        account_id=account_a,
        ma_session_id="sess_old",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="latest-test",
        account_id=account_b,
        ma_session_id="sess_new",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    fetched = await get_latest_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="latest-test",
    )

    assert fetched is not None
    assert fetched.ma_session_id == "sess_new", (
        "get_latest_thread_session must return the newest row regardless of account"
    )


async def test_get_latest_thread_session_ignores_dead_rows(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    account_id = uuid.uuid4()
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="latest-dead-test",
        account_id=account_id,
        ma_session_id="sess_dead",
    )
    await mark_dead(db_session, id=row.id)

    fetched = await get_latest_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="latest-dead-test",
    )

    assert fetched is None, "get_latest_thread_session must exclude dead rows"


async def test_get_latest_thread_session_returns_none_for_unknown_thread(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    fetched = await get_latest_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="unknown-thread",
    )

    assert fetched is None


async def test_healthy_threads_are_never_reported_as_orphans(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """The load-bearing safety property of the whole sweep.

    `status` stays 'live' on every healthy thread forever, so a sweep keyed on
    it would mark every working thread failed. Only an explicit in-flight
    marker may qualify a row.
    """
    await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="healthy-1",
        account_id=uuid.uuid4(),
        ma_session_id="sess_healthy",
    )

    orphans = await list_orphaned_turns(db_session, platform="discord")

    assert orphans == [], "a live row with no in-flight marker is not an orphaned turn"


async def test_marked_turn_is_listed_then_cleared(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="inflight-1",
        account_id=uuid.uuid4(),
        ma_session_id="sess_inflight",
    )
    started = datetime.now(UTC)

    await mark_turn_active(db_session, id=row.id, active_turn_message_id="msg-42", now=started)
    orphans = await list_orphaned_turns(db_session, platform="discord")
    assert [o.id for o in orphans] == [row.id], "a marked row must be reapable after a restart"
    assert orphans[0].active_turn_message_id == "msg-42", (
        "the embed id must survive, since it is the only handle on the frozen message"
    )

    await clear_active_turn(db_session, id=row.id)
    assert await list_orphaned_turns(db_session, platform="discord") == [], (
        "a turn that reached a terminal state must not be reaped later"
    )


async def test_orphan_listing_is_scoped_to_one_platform(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """A Discord boot must not retire Slack's in-flight turns."""
    slack_row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        thread_id="slack-1",
        account_id=uuid.uuid4(),
        ma_session_id="sess_slack",
    )
    await mark_turn_active(
        db_session, id=slack_row.id, active_turn_message_id="slack-msg", now=datetime.now(UTC)
    )

    assert await list_orphaned_turns(db_session, platform="discord") == [], (
        "each adapter reaps only its own platform's turns"
    )
    assert len(await list_orphaned_turns(db_session, platform="slack")) == 1


async def test_slack_marked_turn_lists_back_with_its_channel(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """A Slack message is addressed by (channel, ts); the sweep needs the channel."""
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        thread_id="1700000000.000100",
        account_id=uuid.uuid4(),
        ma_session_id="sess_slack_sweep",
    )

    await mark_turn_active(
        db_session,
        id=row.id,
        active_turn_message_id="1700000000.000200",
        active_turn_channel_id="C_SWEEP",
        now=datetime.now(UTC),
    )

    orphans = await list_orphaned_turns(db_session, platform="slack")
    assert [o.active_turn_channel_id for o in orphans] == ["C_SWEEP"], (
        "the sweep cannot address a Slack message with chat_update without its channel"
    )


async def test_mark_turn_active_without_channel_defaults_to_none(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """Discord's call site passes no channel; a Discord message id is globally addressable."""
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="discord-no-channel",
        account_id=uuid.uuid4(),
        ma_session_id="sess_discord_no_channel",
    )

    await mark_turn_active(
        db_session, id=row.id, active_turn_message_id="msg-99", now=datetime.now(UTC)
    )

    orphans = await list_orphaned_turns(db_session, platform="discord")
    assert orphans[0].active_turn_channel_id is None, (
        "omitting the channel kwarg must store NULL, the correct reading for Discord"
    )


async def test_clear_active_turn_nulls_all_three_marker_columns(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        thread_id="1700000000.000300",
        account_id=uuid.uuid4(),
        ma_session_id="sess_slack_clear",
    )
    await mark_turn_active(
        db_session,
        id=row.id,
        active_turn_message_id="1700000000.000400",
        active_turn_channel_id="C_CLEAR",
        now=datetime.now(UTC),
    )

    await clear_active_turn(db_session, id=row.id)

    cleared = await get_thread_session_by_id(db_session, id=row.id)
    assert cleared is not None, "the row itself must survive a clear, only the marker is reset"
    assert cleared.active_turn_message_id is None, "cleared rows carry no dead message id"
    assert cleared.active_turn_started_at is None, "cleared rows carry no dead start time"
    assert cleared.active_turn_channel_id is None, "cleared rows carry no dead channel id"


async def test_clear_active_turn_if_message_id_clears_when_the_marker_still_matches(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        thread_id="1700000000.000500",
        account_id=uuid.uuid4(),
        ma_session_id="sess_slack_conditional_match",
    )
    await mark_turn_active(
        db_session,
        id=row.id,
        active_turn_message_id="5555.1",
        active_turn_channel_id="C_MATCH",
        now=datetime.now(UTC),
    )

    cleared = await clear_active_turn_if_message_id(
        db_session, id=row.id, expected_message_id="5555.1"
    )

    assert cleared is True, "a marker that still names the caller's read must clear"
    fetched = await get_thread_session_by_id(db_session, id=row.id)
    assert fetched is not None, "the row itself must survive a clear"
    assert fetched.active_turn_message_id is None, "a matched clear must null the message id"
    assert fetched.active_turn_started_at is None, "a matched clear must null the start time"
    assert fetched.active_turn_channel_id is None, "a matched clear must null the channel id"
    assert await list_orphaned_turns(db_session, platform="slack") == [], (
        "a cleared row must not be reported as an orphan"
    )


async def test_clear_active_turn_if_message_id_leaves_a_marker_that_moved(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        thread_id="1700000000.000600",
        account_id=uuid.uuid4(),
        ma_session_id="sess_slack_conditional_moved",
    )
    await mark_turn_active(
        db_session,
        id=row.id,
        active_turn_message_id="6666.1",
        active_turn_channel_id="C_FIRST",
        now=datetime.now(UTC),
    )
    await mark_turn_active(
        db_session,
        id=row.id,
        active_turn_message_id="6666.2",
        active_turn_channel_id="C_SECOND",
        now=datetime.now(UTC),
    )

    cleared = await clear_active_turn_if_message_id(
        db_session, id=row.id, expected_message_id="6666.1"
    )

    assert cleared is False, (
        "a marker written after the caller's read belongs to a live turn and must survive"
    )
    orphans = await list_orphaned_turns(db_session, platform="slack")
    assert [o.id for o in orphans] == [row.id], "the row must still be listed as an orphan"
    assert orphans[0].active_turn_message_id == "6666.2", (
        "the second mark's message id must still be in place, untouched by the stale clear"
    )
    assert orphans[0].active_turn_channel_id == "C_SECOND", (
        "the second mark's channel id must still be in place, untouched by the stale clear"
    )


async def test_clear_active_turn_if_message_id_is_a_no_op_on_an_already_cleared_row(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        thread_id="1700000000.000700",
        account_id=uuid.uuid4(),
        ma_session_id="sess_slack_conditional_already_cleared",
    )
    await mark_turn_active(
        db_session,
        id=row.id,
        active_turn_message_id="7777.1",
        active_turn_channel_id="C_ALREADY",
        now=datetime.now(UTC),
    )
    await clear_active_turn(db_session, id=row.id)

    cleared = await clear_active_turn_if_message_id(
        db_session, id=row.id, expected_message_id="7777.1"
    )

    assert cleared is False, "a NULL marker never equals a string, so this must not raise or match"


async def test_clear_active_turn_if_message_id_never_touches_another_row(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    first = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        thread_id="1700000000.000800",
        account_id=uuid.uuid4(),
        ma_session_id="sess_slack_conditional_row_a",
    )
    second = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        thread_id="1700000000.000900",
        account_id=uuid.uuid4(),
        ma_session_id="sess_slack_conditional_row_b",
    )
    await mark_turn_active(
        db_session,
        id=first.id,
        active_turn_message_id="8888.1",
        active_turn_channel_id="C_ROW_A",
        now=datetime.now(UTC),
    )
    await mark_turn_active(
        db_session,
        id=second.id,
        active_turn_message_id="8888.1",
        active_turn_channel_id="C_ROW_B",
        now=datetime.now(UTC),
    )

    cleared = await clear_active_turn_if_message_id(
        db_session, id=first.id, expected_message_id="8888.1"
    )

    assert cleared is True, "the named row, which genuinely matches, must clear"
    orphans = await list_orphaned_turns(db_session, platform="slack")
    assert [o.id for o in orphans] == [second.id], (
        "the other row, sharing the same message id, must be untouched"
    )
    assert orphans[0].active_turn_message_id == "8888.1", (
        "a colliding message id on a different row must not be nulled by the named row's clear"
    )
    assert orphans[0].active_turn_channel_id == "C_ROW_B", (
        "the other row's channel id must also survive untouched"
    )


def _snapshot(*, model_id: str = "claude-sonnet-5") -> SessionSnapshot:
    """A minimal but fully-validated snapshot; only `model_id` ever varies here."""
    return SessionSnapshot(
        ma_agent_id="agent_research",
        model_id=model_id,
        system_sha256="sys",
        skills_sha256="skills",
        environment_id="env_science",
        repo_url=None,
        repo_branch=None,
        memory_store_id="memstore_7",
        vault_id="vault_9",
        tools_sha256="tools",
        mcp_servers_sha256="mcp",
        env_sha256="env",
        env_file_id="file_env",
        env_resource_id="res_env",
        agent_version=3,
        agent_name="research-bot",
    )


async def test_created_row_returns_the_snapshot_it_was_given(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    snapshot = _snapshot()
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="snapshot-on-create",
        account_id=uuid.uuid4(),
        ma_session_id="sess_snapshot_create",
        effective_config=snapshot,
        identity_fingerprint="ident-1",
        mutable_fingerprint="mut-1",
    )

    assert row.effective_config == snapshot, (
        "the snapshot must survive the JSONB write and come back as a SessionSnapshot"
    )
    assert row.identity_fingerprint == "ident-1", "the identity fingerprint must persist"
    assert row.mutable_fingerprint == "mut-1", "the mutable fingerprint must persist"


async def test_record_snapshot_backfills_a_row_that_had_none(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="snapshot-backfill",
        account_id=uuid.uuid4(),
        ma_session_id="sess_snapshot_backfill",
    )
    assert row.effective_config is None, "a pre-continuity row starts with no snapshot"

    snapshot = _snapshot()
    await record_snapshot(
        db_session,
        id=row.id,
        snapshot=snapshot,
        identity_fingerprint="ident-backfill",
        mutable_fingerprint="mut-backfill",
    )

    refreshed = await get_thread_session_by_id(db_session, id=row.id)
    assert refreshed is not None, "the row must still exist after a backfill"
    assert refreshed.effective_config == snapshot, "the backfilled snapshot must be readable back"
    assert refreshed.identity_fingerprint == "ident-backfill", "both fingerprints are written"


async def test_update_mutable_fingerprint_leaves_the_identity_fingerprint_alone(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="in-place-refresh",
        account_id=uuid.uuid4(),
        ma_session_id="sess_in_place",
        effective_config=_snapshot(),
        identity_fingerprint="ident-frozen",
        mutable_fingerprint="mut-old",
    )
    refreshed_snapshot = _snapshot().model_copy(update={"env_sha256": "env-new"})

    await update_mutable_fingerprint(
        db_session,
        id=row.id,
        snapshot=refreshed_snapshot,
        mutable_fingerprint="mut-new",
    )

    stored = await get_thread_session_by_id(db_session, id=row.id)
    assert stored is not None, "the refreshed row must still exist"
    assert stored.mutable_fingerprint == "mut-new", "an in-place refresh moves the mutable axis"
    assert stored.identity_fingerprint == "ident-frozen", (
        "an in-place refresh must never rewrite the identity the session froze"
    )
    assert stored.effective_config == refreshed_snapshot, (
        "the stored configuration must reflect what the live session now runs"
    )


async def test_exactly_one_row_stays_live_when_a_session_is_superseded(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    account_id = uuid.uuid4()
    old = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="supersede",
        account_id=account_id,
        ma_session_id="sess_old",
        created_at=datetime(2026, 9, 13, 10, 0, tzinfo=UTC),
    )
    new = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="supersede",
        account_id=account_id,
        ma_session_id="sess_new",
        predecessor_id=old.id,
        transfer_file_id="file_bundle",
        transfer_kind="full",
        created_at=datetime(2026, 9, 13, 11, 0, tzinfo=UTC),
    )

    await mark_superseded(db_session, id=old.id, replaced_by_id=new.id)

    live_ids = (
        (
            await db_session.execute(
                select(ThreadSession.id).where(
                    ThreadSession.tenant_id == tenant_id,
                    ThreadSession.thread_id == "supersede",
                    ThreadSession.status == "live",
                )
            )
        )
        .scalars()
        .all()
    )
    assert list(live_ids) == [new.id], "supersede must leave exactly the successor live"

    bound = await get_live_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="supersede",
        account_id=account_id,
    )
    assert bound is not None and bound.id == new.id, "the caller must bind to the successor"
    assert bound.transfer_kind == "full", "the successor records how much of the task it carries"

    superseded = await get_thread_session_by_id(db_session, id=old.id)
    assert superseded is not None, "the superseded row is kept, never deleted"
    assert superseded.status == "superseded", "the old row records why it stopped being live"
    assert superseded.replaced_by_id == new.id, "the old row must name its successor"


async def test_a_retired_row_is_invisible_to_the_caller_scoped_lookup(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    account_id = uuid.uuid4()
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="fresh-start",
        account_id=account_id,
        ma_session_id="sess_retired",
    )

    await mark_retired(db_session, id=row.id)

    assert (
        await get_live_thread_session(
            db_session,
            tenant_id=tenant_id,
            platform="discord",
            thread_id="fresh-start",
            account_id=account_id,
        )
        is None
    ), "a retired session must never be bound again"
    kept = await get_thread_session_by_id(db_session, id=row.id)
    assert kept is not None and kept.status == "retired", "the retired row survives as audit"


async def test_fresh_start_request_is_recorded_then_cleared(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="fresh-start-flag",
        account_id=uuid.uuid4(),
        ma_session_id="sess_fresh_flag",
    )
    requested_at = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

    await request_fresh_start(db_session, id=row.id, at=requested_at)
    flagged = await get_thread_session_by_id(db_session, id=row.id)
    assert flagged is not None, "the flagged row must still exist"
    assert flagged.fresh_start_requested_at == requested_at, (
        "the request is recorded without touching the session, which stays usable"
    )
    assert flagged.status == "live", "requesting a fresh start must not retire anything yet"

    await clear_fresh_start(db_session, id=row.id)
    cleared = await get_thread_session_by_id(db_session, id=row.id)
    assert cleared is not None and cleared.fresh_start_requested_at is None, (
        "an honoured request must be cleared so it is not honoured twice"
    )


async def test_lineage_returns_the_whole_chain_oldest_first(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    account_id = uuid.uuid4()
    first = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="lineage",
        account_id=account_id,
        ma_session_id="sess_lineage_1",
    )
    second = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="lineage",
        account_id=account_id,
        ma_session_id="sess_lineage_2",
        predecessor_id=first.id,
    )
    third = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="lineage",
        account_id=account_id,
        ma_session_id="sess_lineage_3",
        predecessor_id=second.id,
    )

    lineage = await get_lineage(db_session, id=third.id)

    assert [row.ma_session_id for row in lineage] == [
        "sess_lineage_1",
        "sess_lineage_2",
        "sess_lineage_3",
    ], "a lineage reads oldest first and ends at the row asked about"
    assert await get_lineage(db_session, id=first.id) == [
        await get_thread_session_by_id(db_session, id=first.id)
    ], "a session with no predecessor is a lineage of one"
    assert await get_lineage(db_session, id=uuid.uuid4()) == [], (
        "an unknown id is an empty lineage, not an error"
    )


async def test_mark_dead_still_only_changes_status(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """Regression guard: continuity added statuses, it did not touch recovery."""
    account_id = uuid.uuid4()
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="dead-unchanged",
        account_id=account_id,
        ma_session_id="sess_dead",
        effective_config=_snapshot(),
        identity_fingerprint="ident-1",
        mutable_fingerprint="mut-1",
    )

    await mark_dead(db_session, id=row.id)

    dead = await get_thread_session_by_id(db_session, id=row.id)
    assert dead is not None, "mark_dead keeps the row"
    assert dead.status == "dead", "mark_dead still writes exactly 'dead'"
    assert dead.replaced_by_id is None, "a dead session has no successor recorded by mark_dead"
    assert dead.effective_config is not None, (
        "a dead row keeps the configuration it ran, for the recovery path to read"
    )
    assert (
        await get_live_thread_session(
            db_session,
            tenant_id=tenant_id,
            platform="discord",
            thread_id="dead-unchanged",
            account_id=account_id,
        )
        is None
    ), "a dead row stays invisible to the caller-scoped lookup"


async def test_pending_unsaved_work_round_trips_and_the_last_answer_wins(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="unsaved-work",
        account_id=uuid.uuid4(),
        ma_session_id="sess_unsaved",
    )
    assert row.pending_unsaved_work is None, "a new row carries no answer"

    await set_pending_unsaved_work(db_session, id=row.id, choice="leave")
    answered = await get_thread_session_by_id(db_session, id=row.id)
    assert answered is not None and answered.pending_unsaved_work == "leave", (
        "the answer has to survive the turn it was given in"
    )

    await set_pending_unsaved_work(db_session, id=row.id, choice="copy")
    changed = await get_thread_session_by_id(db_session, id=row.id)
    assert changed is not None and changed.pending_unsaved_work == "copy", (
        "a caller who answers again means the newer answer"
    )


async def test_superseding_a_row_clears_the_answer_it_was_given_for(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    account_id = uuid.uuid4()
    old = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="unsaved-work-supersede",
        account_id=account_id,
        ma_session_id="sess_unsaved_old",
    )
    await set_pending_unsaved_work(db_session, id=old.id, choice="leave")
    new = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="unsaved-work-supersede",
        account_id=account_id,
        ma_session_id="sess_unsaved_new",
        predecessor_id=old.id,
    )

    await mark_superseded(db_session, id=old.id, replaced_by_id=new.id)

    closed = await get_thread_session_by_id(db_session, id=old.id)
    assert closed is not None and closed.pending_unsaved_work is None, (
        "the answer governed the replacement that just happened; it must not govern another"
    )


async def test_retiring_a_row_clears_the_answer_it_was_given_for(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="unsaved-work-retire",
        account_id=uuid.uuid4(),
        ma_session_id="sess_unsaved_retire",
    )
    await set_pending_unsaved_work(db_session, id=row.id, choice="copy")

    await mark_retired(db_session, id=row.id)

    retired = await get_thread_session_by_id(db_session, id=row.id)
    assert retired is not None and retired.pending_unsaved_work is None, (
        "a retired row is not live, so it holds no standing answer"
    )
