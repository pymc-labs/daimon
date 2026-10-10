"""The privacy erasure queue survives its account and tenant rows."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_privacy_session_deletes_migration_round_trip(db_session: AsyncSession) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0078_privacy_session_deletes.py"
    spec = importlib.util.spec_from_file_location("migration_privacy_session_deletes", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def run(step: str):
        def inner(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(migration, step)()

        return inner

    async def exists() -> bool:
        result = await db_session.execute(
            text("SELECT to_regclass(current_schema() || '.privacy_session_deletes')")
        )
        return result.scalar_one() is not None

    await conn.run_sync(run("downgrade"))
    assert not await exists()
    await conn.run_sync(run("upgrade"))
    assert await exists()
    await db_session.execute(
        text(
            "INSERT INTO privacy_session_deletes (account_id, tenant_ids, pending_session_ids) "
            "VALUES ('00000000-0000-0000-0000-000000000001', '[]', '{}')"
        )
    )
    await conn.run_sync(run("downgrade"))
    assert not await exists()
    await conn.run_sync(run("upgrade"))
    assert await exists()
