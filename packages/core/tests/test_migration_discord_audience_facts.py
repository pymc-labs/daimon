"""The audience facts migration: down/up cycle, and its DDL matches the models."""

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

_TABLES = ("discord_audience_state", "discord_channel_audience")


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0220_discord_audience_facts.py"
    spec = importlib.util.spec_from_file_location("discord_audience_facts_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_channel_budget_alerts_migration() -> None:
    assert migration().down_revision == "0219_channel_budget_alerts"


@pytest.mark.fresh_schema
async def test_audience_facts_migration_down_up_matches_the_models(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    module = migration()

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            inspector = inspect(sync)
            assert not set(_TABLES) & set(inspector.get_table_names(schema=here))
            assert "role_ids" not in {
                c["name"] for c in inspector.get_columns("workspace_members", schema=here)
            }
            module.upgrade()
            inspector = inspect(sync)
            for table in (*_TABLES, "workspace_members"):
                columns = [c["name"] for c in inspector.get_columns(table, schema=here)]
                assert columns == list(Base.metadata.tables[table].columns.keys()), table
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            ours = (*_TABLES, "role_ids")
            assert not [d for d in differences if any(name in str(d) for name in ours)], differences

    await connection.run_sync(cycle)
