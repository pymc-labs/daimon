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
            with pytest.raises(StoreError, match="already on"):
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
                "delivery": {"delivery_status": "pending"},
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
    with pytest.raises(StoreError, match="no Daimon routine has this id in the selected team"):
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
    assert stored.last_finished_claim_at == claim_now
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
    assert stored.last_finished_claim_at == claim_now
    document = routine_rollback_document([stored], exported_at=NOW)
    assert any("older scheduler" in reason for reason in document["not_final_reasons"])


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


@pytest.mark.parametrize("selection", [[], None, "missing"])
async def test_export_refuses_empty_selection(db_session, selection):
    tenant = await make_tenant(db_session)
    ids = [uuid.uuid4()] if selection == "missing" else selection
    with pytest.raises(StoreError, match="at least one|No routines matched|no Daimon routine"):
        await rollback_rows(db_session, tenant_id=tenant.id, internal_routine_ids=ids)


@pytest.mark.parametrize("rival", ["seed", "resume"])
async def test_two_sessions_seeding_and_resuming_cannot_both_succeed(
    db_engine, db_clean, monkeypatch, rival
):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy.sql.dml import Update

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory.begin() as session:
        tid, rows, entries = await setup(session, count=1)
    locked, competing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def first_seed():
        async with factory.begin() as session:
            scalar = session.scalar

            async def held_write(statement, *args, **kwargs):
                if isinstance(statement, Update):
                    locked.set()
                    await release.wait()
                return await scalar(statement, *args, **kwargs)

            monkeypatch.setattr(session, "scalar", held_write)
            await seed_routines(session, entries, tenant_id=tid, now=NOW, apply=True)

    async def competitor():
        async with factory.begin() as session:
            if rival == "seed":
                scalars = session.scalars

                async def waiting_read(statement, *args, **kwargs):
                    competing.set()
                    return await scalars(statement, *args, **kwargs)

                monkeypatch.setattr(session, "scalars", waiting_read)
                await seed_routines(session, entries, tenant_id=tid, now=NOW, apply=True)
            else:
                execute = session.execute

                async def waiting_write(statement, *args, **kwargs):
                    if isinstance(statement, Update):
                        competing.set()
                    return await execute(statement, *args, **kwargs)

                monkeypatch.setattr(session, "execute", waiting_write)
                await resume_routine(session, rows[0].id, tenant_id=tid, now=NOW)

    first = asyncio.create_task(first_seed())
    second = None
    try:
        await asyncio.wait_for(locked.wait(), 10)
        second = asyncio.create_task(competitor())
        await asyncio.wait_for(competing.wait(), 10)
        assert not first.done() and not second.done()
    finally:
        release.set()
        tasks = [first] if second is None else [first, second]
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
    assert results[0] is None
    assert isinstance(results[1], StoreError)
    async with factory() as session:
        stored = await get_routine(session, rows[0].id, tenant_id=tid)
        assert stored.enabled and stored.next_fire_at == entries[0].internal_next_run
        assert stored.seeded_after == entries[0].last_completed_slot


async def test_handback_preview_apply_and_idempotence(db_session_factory):
    from daimon.core.stores.routine_move import pause_for_handback

    async with db_session_factory.begin() as session:
        tid, rows, entries = await setup(session)
        await seed_routines(session, entries, tenant_id=tid, now=NOW, apply=True)
    ids = [row.internal_routine_id for row in rows]
    async with db_session_factory.begin() as session:
        await pause_for_handback(session, tenant_id=tid, internal_routine_ids=ids, now=NOW)
        assert all(
            row.enabled and row.handback_paused_at is None
            for row in await rollback_rows(session, tenant_id=tid)
        )
    async with db_session_factory.begin() as session:
        await pause_for_handback(
            session, tenant_id=tid, internal_routine_ids=ids, now=NOW, apply=True
        )
    async with db_session_factory.begin() as session:
        captured = await rollback_rows(session, tenant_id=tid)
        for row in captured:
            assert not row.enabled and row.next_fire_at is None
            assert row.handback_enabled is True and row.handback_paused_at == NOW
            assert row.handback_next_fire_at == NOW - timedelta(hours=1)
        await pause_for_handback(
            session,
            tenant_id=tid,
            internal_routine_ids=ids,
            now=NOW + timedelta(hours=2),
            apply=True,
        )
        assert captured == await rollback_rows(session, tenant_id=tid)


@pytest.mark.parametrize("invalid", ["empty", "duplicate", "unknown", "cross-team"])
async def test_handback_batch_refuses_without_partial_pause(db_session_factory, invalid):
    from daimon.core.stores.routine_move import pause_for_handback

    async with db_session_factory.begin() as session:
        tid, rows, entries = await setup(session)
        await seed_routines(session, entries, tenant_id=tid, now=NOW, apply=True)
        ids = [row.internal_routine_id for row in rows]
        if invalid == "empty":
            ids = []
        elif invalid == "duplicate":
            ids.append(ids[0])
        elif invalid == "unknown":
            ids.append(uuid.uuid4())
        else:
            _, other, _ = await setup(session, count=1)
            ids.append(other[0].internal_routine_id)
    with pytest.raises(StoreError):
        async with db_session_factory.begin() as session:
            await pause_for_handback(
                session, tenant_id=tid, internal_routine_ids=ids, now=NOW, apply=True
            )
    async with db_session_factory() as session:
        stored = await rollback_rows(session, tenant_id=tid)
        assert all(row.enabled and row.handback_paused_at is None for row in stored)


@pytest.mark.parametrize("restart", ["seed", "resume"])
async def test_restart_clears_handback_record(db_session, restart):
    from daimon.core.stores.routine_move import pause_for_handback

    tid, rows, entries = await setup(db_session, count=1)
    await pause_for_handback(
        db_session,
        tenant_id=tid,
        internal_routine_ids=[rows[0].internal_routine_id],
        now=NOW,
        apply=True,
    )
    if restart == "seed":
        await seed_routines(db_session, entries, tenant_id=tid, now=NOW, apply=True)
    else:
        await resume_routine(db_session, rows[0].id, tenant_id=tid, now=NOW)
    row = await get_routine(db_session, rows[0].id, tenant_id=tid)
    assert row.enabled
    assert (
        row.handback_paused_at is None
        and row.handback_enabled is None
        and row.handback_next_fire_at is None
    )


async def test_seed_claim_handback_export_preserves_next_unrun_slot(db_session):
    from daimon.core.cron import next_slot_at_or_after
    from daimon.core.stores.routine_move import pause_for_handback

    tid, rows, entries = await setup(db_session, count=1)
    claim_now = datetime.now(UTC) - timedelta(minutes=1)
    completed = claim_now.replace(minute=0, second=0, microsecond=0) - timedelta(days=3)
    values = entries[0].model_dump(mode="json") | {
        "last_completed_slot": completed.isoformat(),
        "internal_next_run": next_slot_at_or_after("0 * * * *", "UTC", completed).isoformat(),
    }
    await db_session.execute(
        update(Routine).where(Routine.id == rows[0].id).values(catch_up_policy="run-once")
    )
    await seed_routines(
        db_session,
        [RoutineSeedEntry.model_validate(values)],
        tenant_id=tid,
        now=claim_now,
        apply=True,
    )
    claimed = (await claim_due_fireable(db_session, now=claim_now))[0]
    assert claimed.last_fired_slot == completed + timedelta(hours=1)
    next_run = (await get_routine(db_session, rows[0].id, tenant_id=tid)).next_fire_at
    await pause_for_handback(
        db_session,
        tenant_id=tid,
        internal_routine_ids=[rows[0].internal_routine_id],
        now=claim_now,
        apply=True,
    )
    before = routine_rollback_document(
        await rollback_rows(db_session, tenant_id=tid), exported_at=claim_now
    )
    assert not before["final"] and before["routines"][0]["in_flight"]
    await record_result(db_session, rows[0].id, tail=None, error=None, expected_row=claimed)
    after = routine_rollback_document(
        await rollback_rows(db_session, tenant_id=tid), exported_at=datetime.now(UTC)
    )
    assert after["final"], after["not_final_reasons"]
    result = after["routines"][0]
    assert result["enabled"] is True
    assert result["expected_next_run"] == next_run.isoformat()
    assert (
        next_slot_at_or_after("0 * * * *", "UTC", datetime.fromisoformat(result["resume_after"]))
        == next_run
    )
    assert datetime.fromisoformat(result["resume_after"]) > claimed.last_fired_slot


async def test_user_enabled_edits_reset_handback_and_recapture_off(db_session):
    from daimon.core.stores.routine_move import pause_for_handback
    from daimon.core.stores.routines import update_routine

    tid, rows, entries = await setup(db_session, count=1)
    rid = rows[0].id
    ids = [rows[0].internal_routine_id]
    await seed_routines(db_session, entries, tenant_id=tid, now=NOW, apply=True)
    await pause_for_handback(
        db_session, tenant_id=tid, internal_routine_ids=ids, now=NOW, apply=True
    )
    captured = await get_routine(db_session, rid, tenant_id=tid)
    assert captured.handback_enabled is True
    # MCP's update tool uses this store entry point rather than resume_routine.
    enabled = await update_routine(db_session, rid, tenant_id=tid, enabled=True)
    assert enabled.enabled
    assert enabled.handback_paused_at is None
    assert enabled.handback_enabled is None and enabled.handback_next_fire_at is None
    await pause_routine(db_session, rid, tenant_id=tid)
    paused = await get_routine(db_session, rid, tenant_id=tid)
    assert paused.handback_paused_at is None
    assert paused.handback_enabled is None and paused.handback_next_fire_at is None
    again = NOW + timedelta(minutes=1)
    await pause_for_handback(
        db_session, tenant_id=tid, internal_routine_ids=ids, now=again, apply=True
    )
    stored = await get_routine(db_session, rid, tenant_id=tid)
    assert stored.handback_paused_at == again
    assert stored.handback_enabled is False
    assert stored.handback_next_fire_at is None
    exported = routine_rollback_document([stored], exported_at=again)
    assert exported["final"]
    assert exported["routines"][0]["enabled"] is False
    assert exported["routines"][0]["expected_next_run"] is None


async def test_plain_pause_clears_an_existing_handback(db_session):
    from daimon.core.stores.routine_move import pause_for_handback

    tid, rows, entries = await setup(db_session, count=1)
    await seed_routines(db_session, entries, tenant_id=tid, now=NOW, apply=True)
    await pause_for_handback(
        db_session,
        tenant_id=tid,
        internal_routine_ids=[rows[0].internal_routine_id],
        now=NOW,
        apply=True,
    )
    paused = await pause_routine(db_session, rows[0].id, tenant_id=tid)
    assert paused.handback_paused_at is None
    assert paused.handback_enabled is None and paused.handback_next_fire_at is None
