"""The Teams admissions migration: up/down/up, and its DDL matches the mapped table."""

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

TABLE = "teams_message_admissions"


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0101_teams_message_admissions.py"
    spec = importlib.util.spec_from_file_location("teams_message_admissions_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_teams_admissions_migration_upgrade_downgrade_upgrade_matches_the_model(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    module = migration()

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            assert TABLE not in inspect(sync).get_table_names(schema=here), "downgrade drops it"
            module.upgrade()
            columns = [c["name"] for c in inspect(sync).get_columns(TABLE, schema=here)]
            assert columns == list(Base.metadata.tables[TABLE].columns.keys()), "column order"
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [diff for diff in differences if TABLE in str(diff)], differences

    await connection.run_sync(cycle)
