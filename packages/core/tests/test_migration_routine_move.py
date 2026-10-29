"""Backfill only the exact legacy import tag and preserve stable handover identity."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Routine
from daimon.core.stores.routines import create_routine, get_routine
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


async def upgrade_from_previous(session: AsyncSession) -> None:
    await session.execute(
        text(
            "ALTER TABLE routines DROP CONSTRAINT uq_routines_tenant_source_id, "
            "DROP COLUMN source_routine_id, DROP COLUMN seeded_after, "
            "DROP COLUMN last_fired_slot, DROP COLUMN last_finished_at, DROP COLUMN last_finished_claim_at, "
            "DROP COLUMN handback_paused_at, DROP COLUMN handback_enabled, DROP COLUMN handback_next_fire_at"
        )
    )
    path = Path(__file__).parents[1] / "alembic/versions/0151_routine_claimed_slot.py"
    spec = importlib.util.spec_from_file_location("routine_move_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = await session.connection()

    def upgrade(sync_connection: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_connection)):
            migration.upgrade()

    await connection.run_sync(upgrade)
    source_path = Path(__file__).parents[1] / "alembic/versions/0161_routine_source_id.py"
    source_spec = importlib.util.spec_from_file_location("routine_source_migration", source_path)
    assert source_spec and source_spec.loader
    source_migration = importlib.util.module_from_spec(source_spec)
    source_spec.loader.exec_module(source_migration)

    def rename(sync_connection: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_connection)):
            source_migration.upgrade()

    await connection.run_sync(rename)


@pytest.mark.fresh_schema
async def test_exact_tag_backfill_and_tenant_unique_identity(db_session):
    tenant = await make_tenant(db_session)
    first, second, crlf = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    prompts = [
        (f"internal-routine-import-v1:{first}\nPrompt", first),
        (f"internal-routine-import-v1:{crlf}\r\nPrompt", crlf),
        (f"internal-routine-import-v1:{str(second).upper()}", second),
        (f"prefix internal-routine-import-v1:{uuid.uuid4()}", None),
        (f"internal-routine-import-v1:{uuid.uuid4()} suffix", None),
        (f"Prompt\ninternal-routine-import-v1:{uuid.uuid4()}", None),
        (f"internal-routine-import-v2:{uuid.uuid4()}", None),
        ("internal-routine-import-v1:not-a-uuid", None),
    ]
    rows = [
        await create_routine(
            db_session,
            tenant_id=tenant.id,
            created_by_user_id=None,
            agent_id="ag",
            agent_name="daimon",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message=prompt,
            enabled=False,
        )
        for prompt, _ in prompts
    ]
    await upgrade_from_previous(db_session)
    for original, (_, expected) in zip(rows, prompts, strict=True):
        current = await get_routine(db_session, original.id, tenant_id=tenant.id)
        assert current.source_routine_id == expected
        assert current.seeded_after is None and current.last_fired_slot is None
    await db_session.execute(
        update(Routine).where(Routine.id == rows[0].id).values(trigger_message="edited")
    )
    assert (
        await get_routine(db_session, rows[0].id, tenant_id=tenant.id)
    ).source_routine_id == first
    other_tenant = await make_tenant(db_session)
    await create_routine(
        db_session,
        tenant_id=other_tenant.id,
        source_routine_id=first,
        created_by_user_id=None,
        agent_id="ag",
        agent_name="daimon",
        cron_expr="0 * * * *",
        timezone_="UTC",
        trigger_message="new",
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await create_routine(
                db_session,
                tenant_id=tenant.id,
                source_routine_id=first,
                created_by_user_id=None,
                agent_id="ag",
                agent_name="daimon",
                cron_expr="0 * * * *",
                timezone_="UTC",
                trigger_message="duplicate",
            )


@pytest.mark.fresh_schema
async def test_duplicate_legacy_tags_refuse_migration(db_session):
    tenant = await make_tenant(db_session)
    source_id = uuid.uuid4()
    for _ in range(2):
        await create_routine(
            db_session,
            tenant_id=tenant.id,
            created_by_user_id=None,
            agent_id="ag",
            agent_name="daimon",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message=f"internal-routine-import-v1:{source_id}",
        )
    with pytest.raises(IntegrityError, match="uq_routines_tenant_" + "internal_id"):
        async with db_session.begin_nested():
            await upgrade_from_previous(db_session)
