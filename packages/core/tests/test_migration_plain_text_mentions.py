"""Migration 0100 adds the plain-text mention switch, off for existing tenants."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_plain_text_mentions_upgrade_leaves_existing_tenants_off(
    db_session: AsyncSession,
) -> None:
    """A tenant that predates the column reads the switch as off after the upgrade."""
    tenant = await make_tenant(db_session)
    await db_session.execute(text("ALTER TABLE tenants DROP COLUMN plain_text_mentions"))
    path = Path(__file__).parents[1] / "alembic/versions/0100_plain_text_mentions.py"
    spec = importlib.util.spec_from_file_location("migration_plain_text_mentions", path)
    assert spec and spec.loader, "the migration module must load"
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    await conn.run_sync(upgrade)
    db_session.expire_all()
    row = await get_tenant(db_session, tenant.id)
    assert row is not None and row.plain_text_mentions is False, (
        "an existing tenant must start with the switch off"
    )
