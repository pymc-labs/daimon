"""The additive usage revision column leaves all historical columns intact."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.pricing import UsageTokens
from daimon.core.stores import usage_events
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_usage_revision_roundtrip_preserves_old_rows_and_old_writers(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await usage_events.record(
        db_session,
        tenant_id=tenant.id,
        platform_user_id="user",
        managed_session_id="session",
        model="model",
        model_usage=UsageTokens(100, 50, 30, 20),
        event_id="event",
    )
    before = await db_session.scalar(
        text("SELECT to_jsonb(u) - 'observation_revision' FROM usage_events u")
    )
    assert await db_session.scalar(text("SELECT observation_revision FROM usage_events")) is None
    await db_session.execute(text("UPDATE usage_events SET observation_revision = 3"))
    path = Path(__file__).parents[1] / "alembic/versions/0076_usage_observation_revision.py"
    spec = importlib.util.spec_from_file_location("usage_revision_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = await db_session.connection()

    def roundtrip(sync_connection: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_connection)):
            migration.downgrade()
            migration.upgrade()

    for _ in range(2):
        await connection.run_sync(roundtrip)
        assert (
            await db_session.scalar(text("SELECT observation_revision FROM usage_events")) is None
        )
        assert (
            await db_session.scalar(
                text("SELECT to_jsonb(u) - 'observation_revision' FROM usage_events u")
            )
            == before
        )
    await usage_events.record(
        db_session,
        tenant_id=tenant.id,
        platform_user_id="user",
        managed_session_id="session",
        model="model",
        model_usage=UsageTokens(0, 0, 0, 0),
        event_id="new-old-writer-event",
    )
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM usage_events WHERE observation_revision IS NULL")
        )
        == 2
    )
