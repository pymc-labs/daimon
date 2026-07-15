"""DB-backed unit tests for `daimon.core.stores.routines`.

Covers CRUD, `record_result`, `claim_due_fireable`, `advance_stale`. Each
test inlines its `create_routine(...)` call (per `guideline:testing`) so
required-field changes break every site that uses them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from daimon.core.cron import next_slot_at_or_after
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.stores.routines import (
    advance_stale,
    claim_due_fireable,
    count_routines_for_principal,
    create_routine,
    delete_for_principal,
    delete_routine,
    get_first_routine_for_principal,
    get_routine,
    list_routines_for_tenant,
    pause_routine,
    record_result,
    resume_routine,
    update_routine,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def tenant_id(db_session: AsyncSession) -> uuid.UUID:
    tenant = await make_tenant(db_session)
    return tenant.id


async def test_create_routine_persists_tenant_id(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await provision_tenant(db_session_factory, platform="discord", workspace_id="g1")
    row = await create_routine(
        db_session,
        created_by_user_id="u1",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="*/5 * * * *",
        timezone_="UTC",
        trigger_message="hi",
        tenant_id=result.tenant_id,
    )
    assert row.tenant_id == result.tenant_id, "create_routine must persist the supplied tenant_id"

    fetched = await get_routine(db_session, row.id)
    assert fetched is not None
    assert fetched.tenant_id == result.tenant_id, "RoutineRow must carry tenant_id on read"


async def test_create_round_trip(db_session: AsyncSession, tenant_id: uuid.UUID) -> None:
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id="u1",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="*/5 * * * *",
        timezone_="UTC",
        trigger_message="hi",
    )
    assert row.cron_expr == "*/5 * * * *", "cron_expr must be persisted"
    assert row.enabled is True, "default enabled=True"

    fetched = await get_routine(db_session, row.id)
    assert fetched is not None, "newly created routine must be fetchable by id"
    assert fetched.id == row.id, "fetched row id must match created row id"


async def test_list_for_tenant_filters(db_session: AsyncSession) -> None:
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)
    await create_routine(
        db_session,
        tenant_id=tenant_a.id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="m1",
    )
    await create_routine(
        db_session,
        tenant_id=tenant_b.id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="m2",
    )

    tenant_a_rows = await list_routines_for_tenant(db_session, tenant_id=tenant_a.id)
    assert len(tenant_a_rows) == 1, "list must return only tenant_a's routines"
    assert tenant_a_rows[0].trigger_message == "m1", "must return the correct routine"


async def test_update_modifies_fields(db_session: AsyncSession, tenant_id: uuid.UUID) -> None:
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="orig",
    )
    updated = await update_routine(
        db_session,
        row.id,
        cron_expr="0 9 * * *",
        timezone_="Asia/Tokyo",
        trigger_message="changed",
        enabled=False,
    )
    assert updated is not None, "update must return the new row"
    assert updated.cron_expr == "0 9 * * *", "cron_expr must be updated"
    assert updated.timezone == "Asia/Tokyo", "timezone must be updated"
    assert updated.trigger_message == "changed", "trigger_message must be updated"
    assert updated.enabled is False, "enabled must be updated to False"


async def test_update_agent_id_and_next_fire_at(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="orig",
    )
    new_fire_at = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    updated = await update_routine(
        db_session,
        row.id,
        agent_id="agent_b",
        next_fire_at=new_fire_at,
    )
    assert updated is not None, "update must return the new row"
    assert updated.agent_id == "agent_b", "agent_id must be updated to agent_b"
    assert updated.next_fire_at == new_fire_at, (
        "next_fire_at must be updated to the supplied datetime"
    )
    assert updated.cron_expr == row.cron_expr, "cron_expr must remain unchanged"
    assert updated.trigger_message == row.trigger_message, "trigger_message must remain unchanged"


async def test_update_with_no_kwargs_returns_unchanged_row(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="orig",
    )
    returned = await update_routine(db_session, row.id)
    assert returned is not None, "update with no kwargs must return the existing row"
    assert returned.id == row.id, "returned row must be the same row"
    assert returned.agent_id == "agent_a", "agent_id must remain unchanged"
    assert returned.cron_expr == "* * * * *", "cron_expr must remain unchanged"


async def test_delete_removes_row(db_session: AsyncSession, tenant_id: uuid.UUID) -> None:
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="m",
    )
    deleted = await delete_routine(db_session, row.id)
    assert deleted is True, "delete returns True when a row was removed"

    missing = await get_routine(db_session, row.id)
    assert missing is None, "deleted routine should not be fetchable"

    deleted_again = await delete_routine(db_session, row.id)
    assert deleted_again is False, "delete on a missing id returns False, not an exception"


async def test_record_result_clears_last_error_on_success(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="m",
    )
    # First, set an error.
    await record_result(db_session, row.id, tail=None, error="boom")
    err_row = await get_routine(db_session, row.id)
    assert err_row is not None
    assert err_row.last_error == "boom", "record_result with error= sets last_error"

    # Then a success: error=None should NULL last_error and set tail.
    await record_result(db_session, row.id, tail="all good", error=None)
    ok_row = await get_routine(db_session, row.id)
    assert ok_row is not None
    assert ok_row.last_error is None, "record_result with error=None must clear last_error"
    assert ok_row.last_result_tail == "all good", "tail must be persisted"


async def test_routine_persists_across_session_reopen(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """Commit + close session + new session reading from same connection sees the row.

    Per the plan's note: schema-per-test fixture binds the session to a single
    connection; we simulate a "new session" by closing this one and opening a
    fresh AsyncSession bound to the same connection. Proves persistence past
    session lifecycle (D-13).
    """
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="durable",
    )
    await db_session.commit()
    bind = db_session.bind
    await db_session.close()

    new_session = AsyncSession(bind=bind, expire_on_commit=False)
    try:
        fetched = await get_routine(new_session, row.id)
        assert fetched is not None, "row must survive session.close + reopen"
        assert fetched.trigger_message == "durable", "trigger_message must be preserved"
    finally:
        await new_session.close()


async def test_claim_due_fireable_picks_routine_in_window(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="m",
        next_fire_at=now - timedelta(minutes=1),
    )
    claimed = await claim_due_fireable(db_session, now=now, max_age=timedelta(minutes=15), limit=20)
    assert len(claimed) == 1, "routine due within window must be claimed"
    assert claimed[0].id == row.id, "claimed routine id must match"

    # After claim, phase 2 must have recomputed next_fire_at to a future slot.
    after_claim = await get_routine(db_session, row.id)
    assert after_claim is not None
    assert after_claim.next_fire_at is not None, "phase 2 recompute must repopulate next_fire_at"
    assert after_claim.next_fire_at > now, "recomputed next_fire_at must be in the future"
    assert after_claim.last_fired_at == now, "claim must stamp last_fired_at"


async def test_claim_excludes_too_old_or_future(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="too-old",
        next_fire_at=now - timedelta(minutes=30),
    )
    await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="future",
        next_fire_at=now + timedelta(minutes=30),
    )
    claimed = await claim_due_fireable(db_session, now=now, max_age=timedelta(minutes=15), limit=20)
    assert claimed == [], "claim window [now-15m, now] must exclude older and future rows"


async def test_advance_stale_recovers_orphans(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    orphan = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="orphan",
        next_fire_at=None,
    )
    touched = await advance_stale(db_session, now=now, max_age=timedelta(minutes=15), limit=200)
    assert touched == 1, "advance_stale must touch the one orphan"

    after = await get_routine(db_session, orphan.id)
    assert after is not None
    assert after.next_fire_at is not None, "orphan recovery must populate next_fire_at"
    assert after.next_fire_at > now, "recovered next_fire_at must be in the future"


# ---------------------------------------------------------------------------
# delete_for_principal — purge orchestrator primitive
# ---------------------------------------------------------------------------


async def test_delete_for_principal_removes_routines_matching_tenant_id_and_external_id(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    # principal A: two routines in the tenant
    await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id="ext_a",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="A1",
    )
    await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id="ext_a",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="A2",
    )
    # principal B: one routine in same tenant — must survive
    await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id="ext_b",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="B1",
    )

    rowcount = await delete_for_principal(db_session, tenant_id=tenant_id, external_id="ext_a")
    assert rowcount == 2, "must delete only A's two routines"

    surviving = await list_routines_for_tenant(db_session, tenant_id=tenant_id)
    assert len(surviving) == 1, "B's routine must survive"
    assert surviving[0].trigger_message == "B1", "surviving routine must be B's"


async def test_delete_for_principal_returns_zero_when_no_routines_match(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    rowcount = await delete_for_principal(db_session, tenant_id=tenant_id, external_id="nobody")
    assert rowcount == 0, "no matching rows must return 0, not raise"


async def test_delete_for_principal_does_not_match_when_tenant_id_differs(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    other_tenant = await make_tenant(db_session)
    await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id="ext_a",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="tenant-only",
    )
    rowcount = await delete_for_principal(
        db_session, tenant_id=other_tenant.id, external_id="ext_a"
    )
    assert rowcount == 0, "wrong tenant_id must not match"
    surviving = await list_routines_for_tenant(db_session, tenant_id=tenant_id)
    assert len(surviving) == 1, "routine must survive a different-tenant-targeted purge"


# ---------------------------------------------------------------------------
# count_routines_for_principal / get_first_routine_for_principal — read-only
# mirrors used by the /privacy cascade preview (daimon.core.privacy).
# ---------------------------------------------------------------------------


async def test_count_routines_for_principal_returns_zero_when_no_routines(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    count = await count_routines_for_principal(
        db_session, tenant_id=tenant_id, external_id="nobody"
    )
    assert count == 0, "no rows match -> count must be 0"


async def test_count_routines_for_principal_counts_rows_when_present(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id="ext_c",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="c1",
    )
    await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id="ext_c",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="c2",
    )
    count = await count_routines_for_principal(db_session, tenant_id=tenant_id, external_id="ext_c")
    assert count == 2, "both seeded routines for ext_c must be counted"


async def test_get_first_routine_for_principal_returns_none_when_no_routines(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    row = await get_first_routine_for_principal(
        db_session, tenant_id=tenant_id, external_id="nobody"
    )
    assert row is None, "no routine match -> must return None, not raise"


async def test_get_first_routine_for_principal_returns_row_when_present(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id="ext_d",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="first",
    )
    row = await get_first_routine_for_principal(
        db_session, tenant_id=tenant_id, external_id="ext_d"
    )
    assert row is not None, "seeded routine must be returned"
    assert row.trigger_message == "first", "must return the row matching the principal"


# ---------------------------------------------------------------------------
# pause_routine / resume_routine + scheduler claim-skip
# ---------------------------------------------------------------------------


async def test_pause_routine_clears_next_fire_at(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    future = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="*/5 * * * *",
        timezone_="UTC",
        trigger_message="m",
        next_fire_at=future,
    )
    paused = await pause_routine(db_session, row.id)
    assert paused is not None, "pause_routine must return the updated row"
    assert paused.enabled is False, "pause_routine must set enabled=False"
    assert paused.next_fire_at is None, "pause_routine must clear next_fire_at to NULL"

    refetched = await get_routine(db_session, row.id)
    assert refetched is not None
    assert refetched.enabled is False, "DB row must reflect enabled=False"
    assert refetched.next_fire_at is None, "DB row must reflect next_fire_at IS NULL"


async def test_pause_routine_unknown_id_returns_none(db_session: AsyncSession) -> None:
    result = await pause_routine(db_session, uuid.uuid4())
    assert result is None, "pause_routine on missing id must return None, not raise"


async def test_pause_routine_idempotent_on_already_paused(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="*/5 * * * *",
        timezone_="UTC",
        trigger_message="m",
        enabled=False,
        next_fire_at=None,
    )
    paused = await pause_routine(db_session, row.id)
    assert paused is not None, "pause_routine on already-paused row must still return the row"
    assert paused.enabled is False, "no-op must keep enabled=False"
    assert paused.next_fire_at is None, "no-op must keep next_fire_at NULL"


async def test_resume_routine_recomputes_next_fire_at(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="*/5 * * * *",
        timezone_="UTC",
        trigger_message="m",
        enabled=False,
        next_fire_at=None,
    )
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    resumed = await resume_routine(db_session, row.id, now=now)
    assert resumed is not None, "resume_routine must return the updated row"
    assert resumed.enabled is True, "resume_routine must set enabled=True"
    expected_next = next_slot_at_or_after("*/5 * * * *", "UTC", now)
    assert resumed.next_fire_at == expected_next, (
        "resume_routine must stamp next_fire_at to the recomputed cron slot"
    )


async def test_resume_routine_unknown_id_returns_none(db_session: AsyncSession) -> None:
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    result = await resume_routine(db_session, uuid.uuid4(), now=now)
    assert result is None, "resume_routine on missing id must return None, not raise"


async def test_paused_routine_not_claimed(db_session: AsyncSession, tenant_id: uuid.UUID) -> None:
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    row = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="claim-me",
        next_fire_at=now - timedelta(minutes=1),
    )
    claimed_before = await claim_due_fireable(
        db_session, now=now, max_age=timedelta(minutes=15), limit=20
    )
    assert any(c.id == row.id for c in claimed_before), (
        "baseline: the enabled routine must be claimable in its window"
    )

    # Re-arm next_fire_at so a second claim window applies, then pause.
    await update_routine(db_session, row.id, next_fire_at=now - timedelta(minutes=1))
    paused = await pause_routine(db_session, row.id)
    assert paused is not None, "pause must succeed"

    claimed_after = await claim_due_fireable(
        db_session, now=now, max_age=timedelta(minutes=15), limit=20
    )
    assert not any(c.id == row.id for c in claimed_after), (
        "claim_due_fireable must skip a paused routine (enabled=False AND next_fire_at IS NULL)"
    )


# ---------------------------------------------------------------------------
# Cross-tenant isolation test (R-8)
# ---------------------------------------------------------------------------


async def test_routines_tenant_isolation(db_session: AsyncSession) -> None:
    # Seed two tenants inline (no shared state between tests)
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)

    # Write under tenant_a
    await create_routine(
        db_session,
        tenant_id=tenant_a.id,
        created_by_user_id="u1",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="*/5 * * * *",
        timezone_="UTC",
        trigger_message="hi",
    )

    # Read under tenant_a — sees own row
    rows_a = await list_routines_for_tenant(db_session, tenant_id=tenant_a.id)
    assert len(rows_a) == 1, "tenant_a read should return its own routine"

    # Read under tenant_b — sees nothing (the isolation boundary)
    rows_b = await list_routines_for_tenant(db_session, tenant_id=tenant_b.id)
    assert len(rows_b) == 0, "tenant_b read must not return tenant_a's routine"

    # Write under tenant_b, re-read tenant_a — still sees only its own
    await create_routine(
        db_session,
        tenant_id=tenant_b.id,
        created_by_user_id="u2",
        agent_id="agent_b",
        agent_name="daimon",
        cron_expr="*/5 * * * *",
        timezone_="UTC",
        trigger_message="hi",
    )
    rows_a_after = await list_routines_for_tenant(db_session, tenant_id=tenant_a.id)
    assert len(rows_a_after) == 1, "tenant_b write must not affect tenant_a reads"
