"""The server skills migration creates its tables and drops them on downgrade."""

import importlib.util
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession

_TABLES = ("server_skills", "server_skill_deployments", "agent_skill_follows")


def _load_migration():
    path = Path(__file__).parents[1] / "alembic/versions/0182_server_skills.py"
    spec = importlib.util.spec_from_file_location("migration_server_skills", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def _present(session: AsyncSession) -> list[bool]:
    return [
        (
            await session.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables"
                    " WHERE table_schema = current_schema() AND table_name = :t)"
                ),
                {"t": t},
            )
        ).scalar_one()
        for t in _TABLES
    ]


@pytest.mark.fresh_schema
async def test_server_skills_migration_round_trips(db_session: AsyncSession) -> None:
    migration = _load_migration()
    conn = await db_session.connection()

    def run(step: str) -> Callable[[Connection], None]:
        def inner(sync_conn: Connection) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(migration, step)()

        return inner

    await conn.run_sync(run("downgrade"))
    assert await _present(db_session) == [False, False, False]
    await conn.run_sync(run("upgrade"))
    assert await _present(db_session) == [True, True, True]
