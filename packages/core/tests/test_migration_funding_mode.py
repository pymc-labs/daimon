"""Upgrade existing tenants safely to the default prepaid policy."""

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
async def test_existing_tenants_default_to_prepaid_on_upgrade(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await db_session.execute(text("ALTER TABLE tenants DROP COLUMN funding_mode"))
    path = Path(__file__).parents[1] / "alembic/versions/0028_tenant_funding_mode.py"
    spec = importlib.util.spec_from_file_location("migration_funding", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def upgrade(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    await conn.run_sync(upgrade)
    row = await get_tenant(db_session, tenant.id)
    assert row is not None and row.funding_mode == "prepaid"
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(text("UPDATE tenants SET funding_mode = 'invalid'"))
