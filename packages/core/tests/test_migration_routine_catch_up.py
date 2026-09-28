"""Upgrade routine policies without changing schedules."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.stores.routines import create_routine, get_routine
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_routine_migration_defaults_existing_rows_to_skip(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    due = datetime(2026, 9, 28, tzinfo=UTC)
    routine = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id=None,
        agent_id="agent",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="run",
        next_fire_at=due,
    )
    await db_session.execute(
        text(
            "ALTER TABLE routines DROP COLUMN catch_up_policy, DROP COLUMN last_skipped_from, "
            "DROP COLUMN last_skipped_until, DROP COLUMN last_skip_reason"
        )
    )
    path = Path(__file__).parents[1] / "alembic/versions/0028_sys074_routine_catch_up.py"
    spec = importlib.util.spec_from_file_location("migration_routine", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def upgrade(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    await conn.run_sync(upgrade)
    row = await get_routine(db_session, routine.id, tenant_id=tenant.id)
    assert row is not None and row.catch_up_policy == "skip"
    assert row.next_fire_at == due
    assert row.last_skipped_from is None
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(text("UPDATE routines SET catch_up_policy = 'invalid'"))
