"""The channel admins migration adds an empty table and an empty role-id column."""

import importlib.util
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

    await conn.run_sync(run("downgrade"))
    assert not await table_exists(), "downgrade drops channel_admins"
    await conn.run_sync(run("upgrade"))
    assert await table_exists(), "upgrade recreates channel_admins"

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
