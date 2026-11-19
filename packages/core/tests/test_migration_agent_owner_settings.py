"""The agent owner/settings migration: down/up cycle, and its DDL matches the model."""

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

# Later revisions that add agent_backend_revision columns, oldest first.
_LATER = ("0198_agent_backend_effort", "0202_agent_backend_environment")


def migration(name: str = "0119_agent_owner_settings") -> ModuleType:
    path = Path(__file__).parents[1] / f"alembic/versions/{name}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_agent_backend_model() -> None:
    assert migration().down_revision == "0118_agent_backend_model"


@pytest.mark.fresh_schema
async def test_agent_owner_settings_migration_down_up_matches_the_model(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    module = migration()
    # Later revisions add columns after this one; cycle them too so column order is the model's.
    later = [migration(name) for name in _LATER]
    revisions = "agent_backend_revision"

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            for step in reversed(later):
                step.downgrade()
            module.downgrade()
            names = {c["name"] for c in inspect(sync).get_columns(revisions, schema=here)}
            assert not names & {"profile", "requires", "thread_mode"}
            assert "agent_owner" not in inspect(sync).get_table_names(schema=here)
            module.upgrade()
            for step in later:
                step.upgrade()
            columns = [c["name"] for c in inspect(sync).get_columns(revisions, schema=here)]
            assert columns == list(Base.metadata.tables[revisions].columns.keys()), "column order"
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [d for d in differences if revisions in str(d) or "agent_owner" in str(d)], (
                differences
            )

    await connection.run_sync(cycle)
