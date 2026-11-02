"""The thread hand-off summary migration: one nullable column, up and down."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0132_thread_handoff_summary.py"
    spec = importlib.util.spec_from_file_location("thread_handoff_summary_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_usage_observation_tenant_index() -> None:
    assert migration().down_revision == "0131_usage_obs_tenant_idx"


@pytest.mark.fresh_schema
async def test_the_column_comes_and_goes_and_matches_the_model(db_session: AsyncSession) -> None:
    connection = await db_session.connection()
    module = migration()
    table = "thread_agent_bindings"

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            columns = {c["name"] for c in inspect(sync).get_columns(table, schema=here)}
            assert "handoff_summary" not in columns
            module.upgrade()
            columns_list = [c["name"] for c in inspect(sync).get_columns(table, schema=here)]
            assert columns_list == list(Base.metadata.tables[table].columns.keys())
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [d for d in differences if table in str(d)], differences

    await connection.run_sync(cycle)
