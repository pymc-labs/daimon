"""The fleet migration: up/down/up, and its DDL matches the mapped tables."""

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

TABLES = {"fleet_run", "fleet_worker"}
#: The fleet tables' migrations, oldest first: the models are their sum.
MIGRATIONS = ("0093_fleet_runs", "0128_fleet_run_end_notice")


def migration(name: str = MIGRATIONS[0]) -> ModuleType:
    path = Path(__file__).parents[1] / f"alembic/versions/{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_fleet_migration_upgrade_downgrade_upgrade_matches_the_models(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    modules = [migration(name) for name in MIGRATIONS]

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        before = set(inspect(sync).get_table_names(schema=here)) - TABLES
        with Operations.context(MigrationContext.configure(sync)):
            for module in reversed(modules):
                module.downgrade()
            assert not TABLES.intersection(inspect(sync).get_table_names(schema=here))
            for module in modules:
                module.upgrade()
            assert sync.scalar(text("SHOW lock_timeout")) == "5s"
            assert set(inspect(sync).get_table_names(schema=here)) - TABLES == before
            for name in TABLES:
                actual = inspect(sync)
                assert [c["name"] for c in actual.get_columns(name, schema=here)] == list(
                    Base.metadata.tables[name].columns.keys()
                )
                assert {
                    check["name"] for check in actual.get_check_constraints(name, schema=here)
                } == {
                    constraint.name
                    for constraint in Base.metadata.tables[name].constraints
                    if constraint.__class__.__name__ == "CheckConstraint"
                }
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [diff for diff in differences if any(name in str(diff) for name in TABLES)]

    await connection.run_sync(cycle)
