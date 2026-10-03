"""The channel budget notice migration round-trips and keeps the budgets."""

import importlib.util
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.testing.factories import make_channel_budget
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0043_channel_budget_notices.py"
    spec = importlib.util.spec_from_file_location("migration_channel_budget_notices", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_downgrade_drops_only_the_notice_key(db_session: AsyncSession) -> None:
    await make_channel_budget(db_session, limit_usd=Decimal("2"))
    await db_session.execute(text("UPDATE channel_budgets SET exhausted_notice_key = '2026-07'"))
    migration = _migration()
    conn = await db_session.connection()

    def downgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.downgrade()

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    await conn.run_sync(downgrade)
    assert (
        await db_session.execute(text("SELECT limit_usd FROM channel_budgets"))
    ).scalar_one() == 2
    await conn.run_sync(upgrade)
    key = await db_session.execute(text("SELECT exhausted_notice_key FROM channel_budgets"))
    assert key.scalar_one() is None, "upgrade starts every budget unclaimed"
