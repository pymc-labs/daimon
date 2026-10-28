"""Repository memory migration round-trip matches the owned schema."""

import importlib.util
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_repository_memory_migration_matches_model(db_session: AsyncSession) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0152_repo_memory.py"
    spec = importlib.util.spec_from_file_location("repo_memory_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(module.revision) <= 32
    connection = await db_session.connection()

    def cycle(sync: Connection) -> None:
        schema = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            assert "repo_memory" not in inspect(sync).get_table_names(schema=schema)
            module.upgrade()
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [
                d for d in differences if "repo_memory" in str(d) or "memory_read_only" in str(d)
            ]

    await connection.run_sync(cycle)


@pytest.mark.fresh_schema
async def test_memory_state_migration_matches_model(db_session: AsyncSession) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0153_repo_memory_state.py"
    spec = importlib.util.spec_from_file_location("repo_memory_state_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(module.revision) <= 32
    connection = await db_session.connection()

    def cycle(sync: Connection) -> None:
        schema = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            assert "repo_memory_sha256" not in {
                c["name"] for c in inspect(sync).get_columns("thread_sessions", schema=schema)
            }
            module.upgrade()
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [d for d in differences if "repo_memory_sha256" in str(d)]

    await connection.run_sync(cycle)
