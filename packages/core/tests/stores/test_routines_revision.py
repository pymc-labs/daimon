"""Independent-transaction races for authorized routine schedule transitions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.cron import next_slot_at_or_after
from daimon.core.errors import StoreError
from daimon.core.posted_controls.routines import apply_routine_action
from daimon.core.stores import routines
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.sql.dml import Update


@pytest.mark.parametrize("op", ["pause", "resume"])
async def test_schedule_transition_rejects_concurrent_cron_timezone_edit(
    db_engine, db_clean, monkeypatch, op
):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    now = datetime(2026, 10, 19, 12, tzinfo=UTC)
    async with factory.begin() as db:
        tenant = await make_tenant(db, platform="discord")
        row = await routines.create_routine(
            db,
            tenant_id=tenant.id,
            created_by_user_id="123",
            agent_id="agent_a",
            agent_name="a",
            cron_expr="*/5 * * * *",
            timezone_="UTC",
            trigger_message="work",
            enabled=op == "pause",
            destination_kind="dm",
            source_provenance_known=True,
        )
    read, release = asyncio.Event(), asyncio.Event()

    async def transition():
        async with factory.begin() as db:
            execute = db.execute

            async def paused_execute(statement, *args, **kwargs):
                if isinstance(statement, Update) and statement.table.name == "routines":
                    read.set()
                    await release.wait()
                return await execute(statement, *args, **kwargs)

            monkeypatch.setattr(db, "execute", paused_execute)
            if op == "pause":
                await routines.pause_routine(db, row.id, tenant_id=tenant.id)
            else:
                await routines.resume_routine(db, row.id, tenant_id=tenant.id, now=now)

    task = asyncio.create_task(transition())
    await asyncio.wait_for(read.wait(), 15)
    slot = next_slot_at_or_after("0 9 * * *", "Asia/Tokyo", now)
    async with factory.begin() as db:
        newer = await routines.update_routine(
            db,
            row.id,
            edited_by=row.created_by_user_id,
            tenant_id=tenant.id,
            cron_expr="0 9 * * *",
            timezone_="Asia/Tokyo",
            next_fire_at=slot,
        )
        assert newer is not None
        await routines.record_result(
            db,
            row.id,
            tail="new revision private answer",
            error=None,
            delivery="pending",
            expected_private=newer,
        )
    release.set()
    with pytest.raises(StoreError, match="changed while being edited"):
        await task
    async with factory() as db:
        current = await routines.get_routine(db, row.id, tenant_id=tenant.id)
    assert current is not None
    assert current.cron_expr == "0 9 * * *" and current.timezone == "Asia/Tokyo"
    assert current.next_fire_at == slot and current.enabled == row.enabled
    assert current.updated_at == newer.updated_at > row.updated_at
    assert current.last_result_tail == "new revision private answer"

    # A fresh read cannot authorize a transition using an older panel snapshot.
    with pytest.raises(StoreError, match="changed while being edited"):
        async with factory.begin() as db:
            if op == "pause":
                await routines.pause_routine(
                    db, row.id, tenant_id=tenant.id, expected_updated_at=row.updated_at
                )
            else:
                await routines.resume_routine(
                    db, row.id, tenant_id=tenant.id, now=now, expected_updated_at=row.updated_at
                )

    # Simulate a backwards wall clock: the SQL revision must still advance.
    async with factory.begin() as db:
        from daimon.core._models import Routine

        future = current.updated_at + timedelta(days=1)
        await db.execute(Update(Routine).where(Routine.id == row.id).values(updated_at=future))
    async with factory.begin() as db:
        changed = (
            await routines.pause_routine(
                db, row.id, tenant_id=tenant.id, expected_updated_at=future
            )
            if op == "pause"
            else await routines.resume_routine(
                db, row.id, tenant_id=tenant.id, now=now, expected_updated_at=future
            )
        )
    assert changed is not None and changed.updated_at > future
    assert changed.last_result_tail is None and changed.last_error is None
    assert changed.delivery_payload is None and changed.delivery_lease_owner is None
    assert changed.delivery_status == "skipped"
    assert changed.next_fire_at == (None if op == "pause" else slot)


@pytest.mark.parametrize("op", ["pause", "resume"])
async def test_panel_transition_uses_the_revision_it_authorized(db_engine, db_clean, op):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    now = datetime(2026, 10, 19, 12, tzinfo=UTC)
    async with factory.begin() as db:
        tenant = await make_tenant(db)
        row = await routines.create_routine(
            db,
            tenant_id=tenant.id,
            created_by_user_id="123",
            agent_id="agent_a",
            agent_name="a",
            cron_expr="*/5 * * * *",
            timezone_="UTC",
            trigger_message="original",
            enabled=op == "pause",
        )

    async def allowed(captured):
        assert captured.updated_at == row.updated_at
        async with factory.begin() as other:
            await routines.update_routine(
                other,
                row.id,
                edited_by=row.created_by_user_id,
                tenant_id=tenant.id,
                trigger_message="newer",
            )
        return True

    with pytest.raises(StoreError, match="changed while being edited"):
        async with factory.begin() as db:
            await apply_routine_action(
                db,
                row.id,
                tenant_id=tenant.id,
                op=op,
                allowed=allowed,
                load=routines.get_routine,
                pause=routines.pause_routine,
                resume=routines.resume_routine,
                now=lambda: now,
            )
    async with factory() as db:
        current = await routines.get_routine(db, row.id, tenant_id=tenant.id)
    assert current is not None and current.enabled == row.enabled
    assert current.trigger_message == "newer" and current.updated_at > row.updated_at
