"""The shared-channel facts migration: down/up cycle, and its DDL matches the models."""

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
    path = Path(__file__).parents[1] / "alembic/versions/0116_shared_channel_facts.py"
    spec = importlib.util.spec_from_file_location("shared_channel_facts_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(sync: Connection, table: str, schema: str | None) -> set[str]:
    return {c["name"] for c in inspect(sync).get_columns(table, schema=schema)}


@pytest.mark.fresh_schema
async def test_shared_channel_facts_migration_down_up_matches_the_models(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    module = migration()

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            assert "shared_channels" not in inspect(sync).get_table_names(schema=here)
            assert "foreign_home_id" not in _columns(sync, "platform_principals", here)
            assert "shared_channel" not in _columns(sync, "turn_origins", here)
            module.upgrade()
            assert "foreign_home_id" in _columns(sync, "platform_principals", here)
            assert "shared_channel" in _columns(sync, "turn_origins", here)
            table = "shared_channels"
            columns = [c["name"] for c in inspect(sync).get_columns(table, schema=here)]
            assert columns == list(Base.metadata.tables[table].columns.keys()), "column order"
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            ours = ("shared_channels", "foreign_home_id", "shared_channel")
            assert not [d for d in differences if any(name in str(d) for name in ours)], differences

    await connection.run_sync(cycle)
