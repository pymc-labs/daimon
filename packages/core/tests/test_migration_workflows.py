"""The workflows migration: down/up cycle, and its DDL matches the models."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core import _workflow_ledger_guard
from daimon.core._models import Base
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0228_workflows.py"
    spec = importlib.util.spec_from_file_location("workflows_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_creates_every_workflow_table() -> None:
    modelled = {name for name in Base.metadata.tables if name.startswith("workflow")}
    assert set(migration().TABLES) == modelled, "one migration creates every workflow table"


def test_the_migration_carries_the_ledger_guard_the_models_install() -> None:
    module = migration()
    assert module.LEDGER_GUARD == _workflow_ledger_guard.INSTALL, "ledger guard drifted"
    assert module.LEDGER_GUARD_REMOVAL == _workflow_ledger_guard.REMOVE, "guard removal drifted"


@pytest.mark.fresh_schema
async def test_workflows_migration_down_up_matches_the_models(db_session: AsyncSession) -> None:
    connection = await db_session.connection()
    module = migration()
    tables = set(module.TABLES)

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            # The real run builds the usage_events indexes concurrently, outside a transaction.
            module.drop_all(concurrently=False)
            inspector = inspect(sync)
            assert not tables & set(inspector.get_table_names(schema=here)), "tables dropped"
            usage = {c["name"] for c in inspector.get_columns("usage_events", schema=here)}
            assert not {"workflow_run_id", "workflow_step_id"} & usage, "usage columns dropped"
            module.create_tables()
            module.create_usage_indexes(concurrently=False)
            inspector = inspect(sync)
            for table in (*tables, "usage_events", "web_threads"):
                columns = [c["name"] for c in inspector.get_columns(table, schema=here)]
                assert columns == list(Base.metadata.tables[table].columns.keys()), table
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            ours = (*tables, "workflow_run_id", "workflow_step_id", "subject")
            assert not [d for d in differences if any(name in str(d) for name in ours)], differences

    await connection.run_sync(cycle)
