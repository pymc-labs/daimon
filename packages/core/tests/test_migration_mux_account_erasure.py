"""0142: usage rows outlive their slot, and erased accounts have a durable fence."""

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

DETACHED = ("usage_observation", "accounting_outbox")
NEW_TABLES = ("accounting_binding_context", "erased_account")


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0142_mux_account_erasure.py"
    spec = importlib.util.spec_from_file_location("mux_account_erasure_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_fleet_removal() -> None:
    assert migration().down_revision == "0141_remove_fleet"


def _slot_foreign_keys(sync: Connection, table: str, schema: str) -> list[str]:
    return [
        fk["name"]
        for fk in inspect(sync).get_foreign_keys(table, schema=schema)
        if fk["referred_table"] == "provider_binding_slot"
    ]


@pytest.mark.fresh_schema
async def test_down_and_up_match_the_model_and_keep_rows(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    connection = await db_session.connection()
    module = migration()

    def cycle(sync: Connection) -> None:
        here = str(sync.scalar(text("SELECT current_schema()")))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            assert not set(NEW_TABLES) & set(inspect(sync).get_table_names(schema=here))
            for table in DETACHED:
                assert _slot_foreign_keys(sync, table, here), f"{table} cascades from its slot"
            module.upgrade()
            names = set(inspect(sync).get_table_names(schema=here))
            assert set(NEW_TABLES) <= names
            for table in DETACHED:
                assert _slot_foreign_keys(sync, table, here) == [], f"{table} is detached"
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [
                d for d in differences if any(t in str(d) for t in (*DETACHED, *NEW_TABLES))
            ]
            # A settlement row with no slot is now valid, and stays when the tenant stays.
            sync.execute(
                text(
                    "INSERT INTO accounting_outbox (binding_id, observation_id, revision,"
                    " tenant_id, row) VALUES ('gone', 'o', 1, :t, '{}'::jsonb)"
                ),
                {"t": tenant.id},
            )

    await connection.run_sync(cycle)
    kept = await db_session.scalar(text("SELECT count(*) FROM accounting_outbox"))
    assert kept == 1


@pytest.mark.fresh_schema
async def test_downgrade_keeps_detached_rows_and_checks_new_ones(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, id=uuid4())
    connection = await db_session.connection()
    module = migration()
    insert = (
        "INSERT INTO accounting_outbox (binding_id, observation_id, revision, tenant_id, row)"
        " VALUES (:b, 'o', 1, :t, '{}'::jsonb)"
    )
    await db_session.execute(text(insert), {"b": "orphan", "t": tenant.id})

    def downgrade(sync: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()

    await connection.run_sync(downgrade)
    assert await db_session.scalar(text("SELECT count(*) FROM accounting_outbox")) == 1
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(text(insert), {"b": "new-orphan", "t": tenant.id})
