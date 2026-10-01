"""The channel admins migration adds an empty table, a role-id column, a wake index and
the channel each thread session runs for."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def _load_migration():
    path = Path(__file__).parents[1] / "alembic/versions/0035_channel_admins.py"
    spec = importlib.util.spec_from_file_location("migration_channel_admins", path)
    assert spec and spec.loader, "the migration file loads"
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.mark.fresh_schema
async def test_channel_admins_migration_round_trips(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    migration = _load_migration()
    conn = await db_session.connection()

    def run(step: str):
        def inner(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(migration, step)()

        return inner

    async def table_exists() -> bool:
        found = await db_session.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = 'channel_admins'"
            )
        )
        return found.first() is not None

    async def waiting_index_exists() -> bool:
        found = await db_session.execute(
            text(
                "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() "
                "AND indexname = 'task_continuations_waiting_idx'"
            )
        )
        return found.first() is not None

    await conn.run_sync(run("downgrade"))
    assert not await table_exists(), "downgrade drops channel_admins"
    assert not await waiting_index_exists(), "downgrade drops the waiting wakes index"
    for ma_session_id, status in (
        ("sess_spent", "live"),
        ("sess_quiet", "live"),
        ("sess_retired", "retired"),
    ):
        await db_session.execute(
            text(
                "INSERT INTO thread_sessions (tenant_id, platform, thread_id, ma_session_id, "
                "status) VALUES (:tenant, 'slack', '1700000000.000100', :sess, :status)"
            ),
            {"tenant": tenant.id, "sess": ma_session_id, "status": status},
        )
    for session_id, event_id, channel_id, occurred_at in (
        ("sess_spent", "e1", "c-old", datetime(2026, 1, 1, tzinfo=UTC)),
        ("sess_spent", "e2", "c-new", datetime(2026, 2, 1, tzinfo=UTC)),
        ("sess_spent", "e3", None, datetime(2026, 3, 1, tzinfo=UTC)),
        ("sess_retired", "e4", "c-old", datetime(2026, 1, 1, tzinfo=UTC)),
    ):
        await db_session.execute(
            text(
                "INSERT INTO usage_events (tenant_id, managed_session_id, event_id, channel_id, "
                "occurred_at) VALUES (:tenant, :sess, :event, :channel, :at)"
            ),
            {
                "tenant": tenant.id,
                "sess": session_id,
                "event": event_id,
                "channel": channel_id,
                "at": occurred_at,
            },
        )
    await conn.run_sync(run("upgrade"))
    assert await table_exists(), "upgrade recreates channel_admins"
    assert await waiting_index_exists(), "upgrade adds the waiting wakes index"
    channels = await db_session.execute(
        text("SELECT ma_session_id, channel_id FROM thread_sessions ORDER BY ma_session_id")
    )
    assert channels.tuples().all() == [
        ("sess_quiet", None),
        ("sess_retired", None),
        ("sess_spent", "c-new"),
    ], (
        "a live session takes its latest attributed spend's channel; one with none, "
        "and every non-live row, stays unknown"
    )

    role_ids = await db_session.execute(
        text("SELECT platform_role_ids FROM accounts WHERE id = :id"), {"id": account.id}
    )
    assert role_ids.scalar_one() == [], "existing accounts start with no role ids"
    count = await db_session.execute(text("SELECT count(*) FROM channel_admins"))
    assert count.scalar_one() == 0, "no channel has admins until one is configured"
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO channel_admins (tenant_id, platform, channel_id) "
                    "VALUES (:tenant, 'cli', 'c1')"
                ),
                {"tenant": tenant.id},
            )
