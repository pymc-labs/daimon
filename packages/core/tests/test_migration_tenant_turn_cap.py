"""A nullable tenant cap leaves existing tenants on the deployment default."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_tenant_turn_cap_upgrade_preserves_default(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await db_session.execute(text("ALTER TABLE tenants DROP CONSTRAINT ck_tenants_turn_cap"))
    await db_session.execute(text("ALTER TABLE tenants DROP COLUMN turn_cap"))
    path = Path(__file__).parents[1] / "alembic/versions/0031_hackathon_tenant_turn_cap.py"
    spec = importlib.util.spec_from_file_location("migration_turn_cap", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def upgrade(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    await conn.run_sync(upgrade)
    row = await get_tenant(db_session, tenant.id)
    assert row is not None and row.turn_cap is None
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(text("UPDATE tenants SET turn_cap = 0"))
