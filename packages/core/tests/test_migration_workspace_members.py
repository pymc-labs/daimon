"""The workspace members migration: down/up cycle, and its DDL matches the model."""

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

_TABLES = ("workspace_members", "workspace_member_syncs")


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0156_workspace_members.py"
    spec = importlib.util.spec_from_file_location("workspace_members_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_web_oidc_tokens_migration() -> None:
    assert migration().down_revision == "0155_web_oidc_tokens"


@pytest.mark.fresh_schema
async def test_workspace_members_migration_down_up_matches_the_model(
    db_session: AsyncSession,
) -> None:
    engine = db_session.bind.engine  # the session's schema, on a connection outside its transaction
    module = migration()

    def cycle(sync: Connection) -> None:
        # The index is built concurrently, which needs alembic to own each transaction.
        context = MigrationContext.configure(sync, opts={"transaction_per_migration": True})
        with Operations.context(context):
            with context.begin_transaction(_per_migration=True):
                module.downgrade()
            here = sync.scalar(text("SELECT current_schema()"))
            assert not set(_TABLES) & set(inspect(sync).get_table_names(schema=here))
            sync.commit()
            with context.begin_transaction(_per_migration=True):
                module.upgrade()
            inspector = inspect(sync)
            for table in _TABLES:
                columns = [c["name"] for c in inspector.get_columns(table, schema=here)]
                assert columns == list(Base.metadata.tables[table].columns.keys()), table
            indexes = {i["name"] for i in inspector.get_indexes("platform_principals", schema=here)}
            assert "ix_platform_principals_account_id" in indexes, "the account lookup is indexed"
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            touched = (*_TABLES, "ix_platform_principals_account_id")
            assert not [d for d in differences if any(t in str(d) for t in touched)], differences
            sync.commit()

    async with engine.connect() as connection:
        await connection.run_sync(cycle)
