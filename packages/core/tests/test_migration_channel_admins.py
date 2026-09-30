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
    path = Path(__file__).parents[1] / "alembic/versions/0034_channel_admins.py"
    spec = importlib.util.spec_from_file_location("migration_channel_admins", path)
    assert spec and spec.loader
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

    await conn.run_sync(run("downgrade"))
    assert (await db_session.execute(text("SELECT to_regclass('channel_admins')"))).scalar() is None
    await conn.run_sync(run("upgrade"))

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
