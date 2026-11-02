"""Live sessions the removed `isolate_context` made are marked to start afresh."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.testing.factories import make_thread_session
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0146_isolated_fresh_start.py"
    spec = importlib.util.spec_from_file_location("migration_isolated_fresh_start", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_only_live_isolated_sessions_are_marked(db_session: AsyncSession) -> None:
    isolated = await make_thread_session(db_session)
    retired = await make_thread_session(db_session)
    plain = await make_thread_session(db_session)
    for row, config, status in (
        (isolated, {"context_isolated": True}, "live"),
        (retired, {"context_isolated": True}, "retired"),
        (plain, {}, "live"),
    ):
        await db_session.execute(
            text(
                "UPDATE thread_sessions SET effective_config = CAST(:config AS jsonb), "
                "status = :status WHERE id = :id"
            ),
            {"config": json.dumps(config), "status": status, "id": row.id},
        )
    migration = _migration()

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    await (await db_session.connection()).run_sync(upgrade)
    marked = await db_session.scalars(
        text(
            "SELECT id FROM thread_sessions "
            "WHERE id = ANY(:ids) AND fresh_start_requested_at IS NOT NULL"
        ),
        {"ids": [isolated.id, retired.id, plain.id]},
    )
    assert set(marked) == {isolated.id}, "only a live isolated session starts afresh"
