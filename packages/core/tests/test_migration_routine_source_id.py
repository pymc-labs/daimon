"""Source identity rename preserves links, tenant uniqueness and the backing index."""

import importlib.util
import uuid
from functools import partial
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.stores.routines import create_routine, get_routine
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_source_identity_down_up_preserves_links_and_unique_index(
    db_session: AsyncSession,
) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0161_routine_source_id.py"
    spec = importlib.util.spec_from_file_location("routine_source_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "0158_watch_mcp_claim"
    assert len(migration.revision) <= 32
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    source_id = uuid.uuid4()
    create = partial(
        create_routine,
        db_session,
        created_by_user_id=None,
        agent_id="ag",
        agent_name="daimon",
        cron_expr="0 * * * *",
        timezone_="UTC",
        trigger_message="run",
        enabled=False,
    )
    row = await create(tenant_id=tenant.id, source_routine_id=source_id)
    await create(tenant_id=other.id, source_routine_id=source_id)
    connection = await db_session.connection()

    def check_and_migrate(sync: Connection) -> None:
        for _ in range(2):
            for step, column, constraint in (
                ("downgrade", migration.LEGACY_COLUMN, migration.LEGACY_CONSTRAINT),
                ("upgrade", "source_routine_id", migration.SOURCE_CONSTRAINT),
            ):
                with Operations.context(MigrationContext.configure(sync)):
                    getattr(migration, step)()
                inspector = inspect(sync)
                assert column in {col["name"] for col in inspector.get_columns("routines")}
                unique = inspector.get_unique_constraints("routines")
                assert any(
                    item["name"] == constraint and item["column_names"] == ["tenant_id", column]
                    for item in unique
                )
                assert constraint in {index["name"] for index in inspector.get_indexes("routines")}
                assert (
                    sync.scalar(
                        text(f"SELECT {column} FROM routines WHERE id = :id"), {"id": row.id}
                    )
                    == source_id
                )

    await connection.run_sync(check_and_migrate)
    stored = await get_routine(db_session, row.id, tenant_id=tenant.id)
    assert stored is not None
    assert stored.source_routine_id == source_id
    with pytest.raises(IntegrityError, match="uq_routines_tenant_source_id"):
        async with db_session.begin_nested():
            await create(tenant_id=tenant.id, source_routine_id=source_id)
