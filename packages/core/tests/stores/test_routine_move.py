"""Database handover safety: atomic seed, claim evidence and read-only export."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core._models import Routine
from daimon.core.errors import StoreError
from daimon.core.routine_move import RoutineSeedEntry, routine_rollback_document
from daimon.core.stores.routine_move import rollback_rows, seed_routines
from daimon.core.stores.routines import (
    claim_due_fireable,
    create_routine,
    get_routine,
    pause_routine,
    record_result,
    resume_routine,
)
from daimon.testing.factories import make_tenant
from sqlalchemy import text, update

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


async def setup(session, *, count=2):
    tenant = await make_tenant(session)
    rows = [
        await create_routine(
            session,
            tenant_id=tenant.id,
            created_by_user_id=None,
            agent_id="ag",
            internal_routine_id=uuid.uuid4(),
            agent_name="daimon",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="run",
            enabled=False,
        )
        for _ in range(count)
    ]
    entries = [
        RoutineSeedEntry.model_validate(
            dict(
                internal_routine_id=row.internal_routine_id,
                last_completed_slot="2026-10-27T10:00:00Z",
                internal_next_run="2026-10-27T11:00:00Z",
                cron_expr="0 * * * *",
                timezone="UTC",
            )
        )
        for row in rows
    ]
    return tenant.id, rows, entries


async def test_seed_dry_run_then_apply_and_resume_refuses(db_session_factory):
    async with db_session_factory.begin() as session:
        tid, rows, entries = await setup(session)
    async with db_session_factory.begin() as session:
        planned = await seed_routines(session, entries, tenant_id=tid, now=NOW)
        assert all(slot == NOW - timedelta(hours=1) for _, slot in planned)
        assert all(not row.enabled for row in await rollback_rows(session, tenant_id=tid))
    async with db_session_factory.begin() as session:
        await seed_routines(session, entries, tenant_id=tid, now=NOW, apply=True)
    async with db_session_factory.begin() as session:
        for row in rows:
            stored = await get_routine(session, row.id, tenant_id=tid)
            assert stored.enabled
            assert stored.seeded_after == entries[0].last_completed_slot
            assert stored.next_fire_at == NOW - timedelta(hours=1)
            with pytest.raises(StoreError, match="already enabled"):
                await resume_routine(session, row.id, tenant_id=tid, now=NOW)


@pytest.mark.parametrize(
    "invalid",
    [
        "unknown",
        "duplicate",
        "cross-tenant",
        "fired",
        "delivery",
        "enabled",
        "mismatch",
        "clock",
        "os-id",
    ],
)
async def test_batch_refusal_writes_nothing(db_session_factory, invalid):
    async with db_session_factory.begin() as session:
        tid, rows, entries = await setup(session)
        if invalid == "os-id":
            entries[1] = entries[1].model_copy(update={"internal_routine_id": rows[1].id})
        elif invalid in ("unknown", "cross-tenant"):
            other_id = uuid.uuid4()
            if invalid == "cross-tenant":
                _, other, _ = await setup(session, count=1)
                other_id = other[0].internal_routine_id
            entries[1] = entries[1].model_copy(update={"internal_routine_id": other_id})
        elif invalid == "duplicate":
            entries.append(entries[0])
        elif invalid == "clock":
            entries[1] = entries[1].model_copy(update={"internal_next_run": NOW})
        elif invalid == "mismatch":
            entries[1] = entries[1].model_copy(update={"timezone": "America/New_York"})
        else:
            values = {
                "fired": {"last_fired_at": NOW},
                "delivery": {"delivery_status": "skipped"},
                "enabled": {"enabled": True},
            }[invalid]
            await session.execute(update(Routine).where(Routine.id == rows[1].id).values(**values))
    with pytest.raises(StoreError):
        async with db_session_factory.begin() as session:
            await seed_routines(session, entries, tenant_id=tid, now=NOW, apply=True)
    async with db_session_factory() as session:
        stored = await get_routine(session, rows[0].id, tenant_id=tid)
        assert stored.enabled is False and stored.next_fire_at is None


async def test_run_once_retains_claimed_slot_and_completion(db_session):
    tid, rows, entries = await setup(db_session, count=1)
    claim_now = datetime.now(UTC) - timedelta(minutes=1)
    slot = claim_now - timedelta(days=3)
    await db_session.execute(
        update(Routine)
        .where(Routine.id == rows[0].id)
        .values(
            enabled=True,
            catch_up_policy="run-once",
            next_fire_at=slot,
            last_finished_at=NOW,
        )
    )
    claimed = await claim_due_fireable(db_session, now=claim_now)
    assert len(claimed) == 1
    assert claimed[0].last_finished_at is None
    assert claimed[0].last_fired_slot == slot
    assert claimed[0].last_fired_at == claim_now
    await pause_routine(db_session, rows[0].id, tenant_id=tid)
    before = routine_rollback_document(
        await rollback_rows(db_session, tenant_id=tid), exported_at=NOW
    )
    assert not before["final"] and before["routines"][0]["in_flight"]
    await record_result(db_session, rows[0].id, tail=None, error="failed", expected_row=claimed[0])
    after = routine_rollback_document(
        await rollback_rows(db_session, tenant_id=tid), exported_at=NOW
    )
    assert after["final"] and not after["routines"][0]["in_flight"]
    assert after["routines"][0]["resume_after"] == slot.isoformat()


async def test_export_works_in_read_only_transaction(db_session_factory):
    async with db_session_factory.begin() as session:
        tid, rows, _ = await setup(session)
        await session.execute(
            update(Routine).where(Routine.tenant_id == tid).values(seeded_after=NOW)
        )
    async with db_session_factory.begin() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        before = (await session.execute(text("SELECT * FROM routines ORDER BY id"))).all()
        exported = routine_rollback_document(
            await rollback_rows(session, tenant_id=tid), exported_at=NOW
        )
        after = (await session.execute(text("SELECT * FROM routines ORDER BY id"))).all()
        assert before == after
        assert exported["final"] and len(exported["routines"]) == len(rows)


async def test_export_filters_refuse_cross_tenant(db_session):
    tid, rows, _ = await setup(db_session)
    other_tid, _, _ = await setup(db_session, count=1)
    with pytest.raises(StoreError, match="no routine in this tenant"):
        await rollback_rows(
            db_session, tenant_id=other_tid, internal_routine_ids=[rows[0].internal_routine_id]
        )


async def test_completion_is_claim_fenced_even_when_content_revision_changes(db_session):
    tid, rows, _ = await setup(db_session, count=1)
    claim_now = datetime.now(UTC) - timedelta(minutes=2)
    rid = rows[0].id
    await db_session.execute(
        update(Routine)
        .where(Routine.id == rid)
        .values(
            enabled=True,
            next_fire_at=claim_now,
        )
    )
    claimed = (await claim_due_fireable(db_session, now=claim_now))[0]
    # Simulate an edit rejecting the old turn's content, but not its completion.
    await db_session.execute(
        update(Routine)
        .where(Routine.id == rid)
        .values(
            updated_at=claimed.updated_at + timedelta(seconds=1),
        )
    )
    await record_result(db_session, rid, tail="obsolete result", error=None, expected_row=claimed)
    stored = await get_routine(db_session, rid, tenant_id=tid)
    assert stored.last_result_tail is None
    assert stored.last_finished_at >= claim_now
    completed_at = stored.last_finished_at
    # An older private turn must not mark a later claim finished either.
    newer_claim = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.execute(
        update(Routine)
        .where(Routine.id == rid)
        .values(
            last_fired_at=newer_claim,
        )
    )
    await record_result(db_session, rid, tail=None, error="old", expected_private=claimed)
    stored = await get_routine(db_session, rid, tenant_id=tid)
    assert stored.last_finished_at == completed_at


@pytest.mark.fresh_schema
async def test_several_matches_refuse_the_entire_batch(db_session):
    tid, rows, entries = await setup(db_session)
    # Defensive refusal even for a legacy/corrupted DB without the unique constraint.
    await db_session.execute(
        text("ALTER TABLE routines DROP CONSTRAINT uq_routines_tenant_internal_id")
    )
    await db_session.execute(
        update(Routine)
        .where(Routine.id == rows[1].id)
        .values(
            internal_routine_id=rows[0].internal_routine_id,
        )
    )
    with pytest.raises(StoreError, match="several routines match"):
        await seed_routines(db_session, entries[:1], tenant_id=tid, now=NOW, apply=True)
    assert not (await get_routine(db_session, rows[0].id, tenant_id=tid)).enabled
