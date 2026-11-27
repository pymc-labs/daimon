"""Migration 0232 adds where an agent-chat session came from, and a retry finishes it.

Its index is built outside the migration's transaction, so a run cut short there leaves
the columns committed and the index missing or invalid. The same upgrade then runs again.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0232_agent_chat_session_grant.py"
    spec = importlib.util.spec_from_file_location("migration_agent_chat_session_grant", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _index_valid(sync: Connection, name: str) -> bool | None:
    """The index's validity in this test's schema only; None when it is missing."""
    return sync.scalar(
        text(
            "SELECT i.indisvalid FROM pg_index i"
            " JOIN pg_class c ON c.oid = i.indexrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.relname = :name AND n.nspname = current_schema()"
        ),
        {"name": name},
    )


@pytest.mark.fresh_schema
async def test_a_retry_after_an_interrupted_run_finishes_the_migration(
    db_session: AsyncSession,
) -> None:
    """With the index missing, then left invalid by a failed build, the retry ends with the
    model's columns and a valid index. A column of another type is refused. One test, so
    the schema these DDL commits change is rebuilt once."""
    migration = _migration()
    table, index = migration.TABLE, migration.INDEX
    connection = await db_session.connection()

    def interrupted_then_retried(sync: Connection, left: str) -> None:
        sync.commit()
        context = MigrationContext.configure(sync)
        with context.begin_transaction(), Operations.context(context):
            migration.downgrade()
            # The cut-short run: its ALTER committed; its index build did not finish.
            migration.add_columns(sync)
            if left == "invalid_index":
                with context.autocommit_block():
                    sync.execute(
                        text(
                            f"INSERT INTO {table} (managed_session_id, agent_chat_grant) "
                            "VALUES ('sesn-a', 'shared'), ('sesn-b', 'shared')"
                        )
                    )
                    # Two rows share a grant, so the build fails and leaves its index.
                    with pytest.raises(IntegrityError):
                        sync.execute(
                            text(
                                f"CREATE UNIQUE INDEX CONCURRENTLY {index} ON {table} "
                                "(agent_chat_grant) WHERE agent_chat_grant IS NOT NULL"
                            )
                        )
                    assert _index_valid(sync, index) is False
                    sync.execute(text(f"DELETE FROM {table} WHERE managed_session_id = 'sesn-b'"))
            else:
                assert _index_valid(sync, index) is None
            migration.upgrade()
            assert _index_valid(sync, index) is True, left
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [diff for diff in differences if table in str(diff)], differences

    def wrong_type(sync: Connection) -> None:
        context = MigrationContext.configure(sync)
        with Operations.context(context):
            migration.downgrade()
            sync.execute(text(f"ALTER TABLE {table} ADD COLUMN agent_chat_grant integer"))
            with pytest.raises(RuntimeError, match="agent_chat_grant exists as integer, not text"):
                migration.add_columns(sync)

    for left in ("no_index", "invalid_index"):
        await connection.run_sync(interrupted_then_retried, left)
    await connection.run_sync(wrong_type)
