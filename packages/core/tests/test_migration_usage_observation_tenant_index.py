"""The usage_observation tenant index migration: down/up cycle, its DDL matches the model."""

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from daimon.core._models import Base
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0131_usage_obs_tenant_idx.py"
    spec = importlib.util.spec_from_file_location("usage_observation_tenant_index", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_mains_head() -> None:
    assert migration().down_revision == "0128_fleet_run_end_notice"


async def test_the_tenant_index_exists_and_matches_the_model(db_session: AsyncSession) -> None:
    """Built concurrently outside a transaction, so `alembic upgrade head` (the test DB's
    own migration) is its real exercise; here the result must match the model."""
    connection = await db_session.connection()
    table = "usage_observation"

    def check(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        names = {index["name"] for index in inspect(sync).get_indexes(table, schema=here)}
        assert migration().INDEX in names
        differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
        assert not [d for d in differences if table in str(d)], differences

    await connection.run_sync(check)
