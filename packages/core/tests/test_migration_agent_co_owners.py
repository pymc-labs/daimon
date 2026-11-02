"""The agent co-owners migration: down/up cycle, and its DDL matches the model."""

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
    path = Path(__file__).parents[1] / "alembic/versions/0143_agent_co_owners.py"
    spec = importlib.util.spec_from_file_location("agent_co_owners_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_mux_account_erasure_migration() -> None:
    assert migration().down_revision == "0142_mux_account_erasure"


@pytest.mark.fresh_schema
async def test_agent_co_owners_migration_down_up_matches_the_model(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    module = migration()

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            assert "agent_co_owners" not in inspect(sync).get_table_names(schema=here)
            assert "agent_owner" in inspect(sync).get_table_names(schema=here)
            module.upgrade()
            inspector = inspect(sync)
            columns = [c["name"] for c in inspector.get_columns("agent_co_owners", schema=here)]
            assert columns == list(Base.metadata.tables["agent_co_owners"].columns.keys())
            key = inspector.get_pk_constraint("agent_co_owners", schema=here)
            assert key["constrained_columns"] == ["tenant_id", "agent_id", "account_id"]
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [d for d in differences if "agent_co_owners" in str(d)], differences

    await connection.run_sync(cycle)
