"""The turn memory grants migration: down/up cycle, its DDL matches the model."""

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
    path = Path(__file__).parents[1] / "alembic/versions/0126_turn_memory_grants.py"
    spec = importlib.util.spec_from_file_location("turn_memory_grants_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_main_head() -> None:
    assert migration().down_revision == "0123_tenant_backends_explicit"


@pytest.mark.fresh_schema
async def test_turn_memory_grants_migration_down_up_matches_the_model(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    module = migration()
    table = "turn_memory_grants"

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            assert table not in inspect(sync).get_table_names(schema=here)
            module.upgrade()
            columns = [c["name"] for c in inspect(sync).get_columns(table, schema=here)]
            assert columns == list(Base.metadata.tables[table].columns.keys()), "column order"
            fks = {
                fk["referred_table"] for fk in inspect(sync).get_foreign_keys(table, schema=here)
            }
            assert {"mcp_tokens", "tenants"} <= fks
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [d for d in differences if table in str(d)], differences

    await connection.run_sync(cycle)
