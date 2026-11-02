"""The MCP token surface migration: down/up cycle, its DDL matches the model."""

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
    path = Path(__file__).parents[1] / "alembic/versions/0120_mcp_token_surface.py"
    spec = importlib.util.spec_from_file_location("mcp_token_surface_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_agent_owner_settings() -> None:
    assert migration().down_revision == "0119_agent_owner_settings"


@pytest.mark.fresh_schema
async def test_mcp_token_surface_migration_down_up_matches_the_model(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    module = migration()
    table = "mcp_tokens"

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            columns = {c["name"] for c in inspect(sync).get_columns(table, schema=here)}
            assert "surface" not in columns
            module.upgrade()
            columns = [c["name"] for c in inspect(sync).get_columns(table, schema=here)]
            # Later columns (`purpose`, 0143) were added after `surface` was dropped and
            # re-added here, so the order is checked without them.
            later = {"purpose"}
            assert [c for c in columns if c not in later] == [
                c.name for c in Base.metadata.tables[table].columns if c.name not in later
            ], "column order"
            checks = {c["name"] for c in inspect(sync).get_check_constraints(table, schema=here)}
            assert "ck_mcp_tokens_surface" in checks
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [d for d in differences if table in str(d)], differences

    await connection.run_sync(cycle)
