"""Historical outcome rows survive telemetry upgrade and downgrade."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import TurnOutcome
from daimon.core.stores.turn_usage import list_turn_usage, usage_by_channel
from daimon.testing.factories import make_tenant
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_usage_migration_keeps_historical_metrics_unknown(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    now = datetime.now(UTC)
    turn_id = uuid4()
    await db_session.execute(
        insert(TurnOutcome).values(
            id=turn_id,
            tenant_id=tenant.id,
            platform="discord",
            origin="chat",
            reason="completed",
            started_at=now,
            ended_at=now,
            duration_ms=0,
            recovered=False,
            release="test",
            usage_refs=[],
        )
    )
    path = Path(__file__).parents[1] / "alembic/versions/0030_sys066_turn_usage.py"
    spec = importlib.util.spec_from_file_location("usage_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def roundtrip(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.downgrade()
            migration.upgrade()

    await conn.run_sync(roundtrip)
    rows = await list_turn_usage(db_session, tenant_id=tenant.id, since=now - timedelta(days=1))
    assert len(rows) == 1 and rows[0].id == turn_id
    assert rows[0].model_calls is None and rows[0].cost_usd is None
    groups = await usage_by_channel(db_session, tenant_id=tenant.id, since=now - timedelta(days=1))
    assert groups[0].turns == 1 and groups[0].measured_turns == 0
    assert groups[0].cost_usd is None
    assert await db_session.scalar(text("SELECT count(*) FROM turn_outcomes")) == 1
