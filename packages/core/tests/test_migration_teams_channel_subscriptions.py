"""Migration 0101 creates the channel subscription table, and its downgrade drops it."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.stores.teams_channel_subscriptions import list_teams_channel_subscriptions
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_teams_channel_subscriptions_upgrade_matches_the_model(
    db_session: AsyncSession,
) -> None:
    """Rebuilt by the migration, the table serves the store; downgrade removes it."""
    tenant = await make_tenant(db_session)
    await db_session.execute(text("DROP TABLE teams_channel_subscriptions"))
    path = Path(__file__).parents[1] / "alembic/versions/0101_teams_channel_subscriptions.py"
    spec = importlib.util.spec_from_file_location("migration_teams_subscriptions", path)
    assert spec and spec.loader, "the migration module must load"
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def run(sync_conn: Connection, step: str) -> bool:
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(migration, step)()
        return inspect(sync_conn).has_table("teams_channel_subscriptions")

    assert await conn.run_sync(run, "upgrade"), "upgrade creates the table"
    rows = await list_teams_channel_subscriptions(db_session, tenant_id=tenant.id)
    assert rows == [], "the table starts empty"
    assert not await conn.run_sync(run, "downgrade"), "downgrade drops it"
