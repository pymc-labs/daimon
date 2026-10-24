"""The agent backend model migration: down/up cycle, and its DDL matches the model."""

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


def migration(name: str = "0118_agent_backend_model") -> ModuleType:
    path = Path(__file__).parents[1] / f"alembic/versions/{name}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_channel_memory_store() -> None:
    assert migration().down_revision == "0117_agent_channel_memory_store"


@pytest.mark.fresh_schema
async def test_agent_backend_model_migration_down_up_matches_the_model(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    module = migration()
    # 0119 adds columns after this one; cycle it too so column order is the model's.
    later = migration("0119_agent_owner_settings")
    table = "agent_backend_revision"

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            later.downgrade()
            module.downgrade()
            assert "model" not in {c["name"] for c in inspect(sync).get_columns(table, schema=here)}
            module.upgrade()
            later.upgrade()
            columns = [c["name"] for c in inspect(sync).get_columns(table, schema=here)]
            assert columns == list(Base.metadata.tables[table].columns.keys()), "column order"
            (model,) = [
                c for c in inspect(sync).get_columns(table, schema=here) if c["name"] == "model"
            ]
            assert model["nullable"] is True
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [d for d in differences if table in str(d)], differences

    await connection.run_sync(cycle)
