"""The routine delivery hold migration: down/up cycle, and its DDL matches the models."""

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
    path = Path(__file__).parents[1] / "alembic/versions/0221_routine_delivery_hold.py"
    spec = importlib.util.spec_from_file_location("routine_delivery_hold_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_audience_facts_migration() -> None:
    assert migration().down_revision == "0220_discord_audience_facts"


@pytest.mark.fresh_schema
async def test_routine_delivery_hold_migration_down_up_matches_the_models(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    module = migration()

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            columns = {c["name"] for c in inspect(sync).get_columns("routines", schema=here)}
            assert "delivery_hold" not in columns
            module.upgrade()
            columns = [c["name"] for c in inspect(sync).get_columns("routines", schema=here)]
            assert columns == list(Base.metadata.tables["routines"].columns.keys())
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [d for d in differences if "delivery_hold" in str(d)], differences

    await connection.run_sync(cycle)
