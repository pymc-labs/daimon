"""Forward removal deletes Fleet-only history while retaining shared ordinary data."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import TurnOutcome
from daimon.core.stores.turn_usage import list_turn_usage
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, insert, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_forward_removal_preserves_shared_history_and_refuses_new_worker_rows(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    now = datetime.now(UTC)
    historical_id, ordinary_id, run_id = uuid4(), uuid4(), uuid4()
    # Recreate only removed storage in a private throwaway schema. CI's Alembic
    # upgrade additionally exercises the complete immutable migration chain.
    for ddl in (
        "CREATE TABLE fleet_run (id uuid PRIMARY KEY, tenant_id uuid REFERENCES tenants(id))",
        "CREATE TABLE fleet_worker (id uuid PRIMARY KEY, run_id uuid REFERENCES fleet_run(id))",
        "CREATE INDEX fleet_worker_run_idx ON fleet_worker(run_id)",
        "ALTER TABLE agent_backend_revision ADD COLUMN fleet_grant jsonb",
        "ALTER TABLE turn_outcomes DROP CONSTRAINT ck_turn_outcomes_origin",
        "ALTER TABLE turn_outcomes ADD CONSTRAINT ck_turn_outcomes_origin "
        "CHECK (origin IN ('chat', 'routine', 'relay', 'handoff', 'worker'))",
    ):
        await db_session.execute(text(ddl))
    await db_session.execute(
        text("INSERT INTO fleet_run VALUES (:id, :tenant)"), {"id": run_id, "tenant": tenant.id}
    )
    await db_session.execute(
        text("INSERT INTO fleet_worker VALUES (:id, :run)"), {"id": uuid4(), "run": run_id}
    )
    for identity, origin in ((historical_id, "worker"), (ordinary_id, "chat")):
        await db_session.execute(
            insert(TurnOutcome).values(
                id=identity,
                tenant_id=tenant.id,
                platform="discord",
                origin=origin,
                reason="completed",
                started_at=now,
                ended_at=now,
                duration_ms=0,
                recovered=False,
                release="test",
                usage_refs=[],
            )
        )
    path = Path(__file__).parents[1] / "alembic/versions/0141_remove_fleet.py"
    spec = importlib.util.spec_from_file_location("remove_fleet_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = await db_session.connection()

    def upgrade(sync: Connection) -> None:
        schema = sync.scalar(text("SELECT current_schema()"))
        before = set(inspect(sync).get_table_names(schema=schema))
        with Operations.context(MigrationContext.configure(sync)):
            migration.upgrade()
        inspector = inspect(sync)
        assert set(inspector.get_table_names(schema=schema)) == before - {
            "fleet_run",
            "fleet_worker",
        }
        assert "fleet_grant" not in {
            column["name"]
            for column in inspector.get_columns("agent_backend_revision", schema=schema)
        }
        checks = sync.execute(
            text(
                "SELECT conname, convalidated FROM pg_constraint "
                "WHERE connamespace=current_schema()::regnamespace "
                "AND conname IN ('ck_turn_outcomes_origin', 'ck_task_continuations_reason')"
            )
        ).all()
        assert set(checks) == {
            ("ck_turn_outcomes_origin", False),
            ("ck_task_continuations_reason", False),
        }

    await connection.run_sync(upgrade)
    rows = await list_turn_usage(db_session, tenant_id=tenant.id, since=now - timedelta(days=1))
    assert {(row.id, row.origin) for row in rows} == {
        (historical_id, "worker"),
        (ordinary_id, "chat"),
    }
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE turn_outcomes SET origin='worker' WHERE id=:id"),
                {"id": ordinary_id},
            )
    assert await db_session.scalar(text("SELECT count(*) FROM turn_outcomes")) == 2
