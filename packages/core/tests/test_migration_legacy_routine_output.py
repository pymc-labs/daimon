"""Purge persisted legacy private previews without touching routed results or schedules."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.stores.routines import create_routine, get_routine
from daimon.testing.factories import make_tenant
from sqlalchemy import text


@pytest.mark.parametrize("enabled", [False, True])
async def test_purge_legacy_output_and_noop_downgrade(db_session, enabled):
    tenant = await make_tenant(db_session)
    rows = []
    for destination in (
        {},
        {"channel_id": "456"},
        {"destination_kind": "channel", "destination_id": "456"},
        {"destination_kind": "dm", "destination_id": "123"},
    ):
        row = await create_routine(
            db_session,
            tenant_id=tenant.id,
            created_by_user_id="123",
            agent_id="ag",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="legacy",
            enabled=enabled,
            **destination,
        )
        rows.append(row)
        await db_session.execute(
            text(
                "UPDATE routines SET last_result_tail = 'private-tail', last_error = 'private-error', "
                "delivery_payload = 'private-payload', delivery_note = 'private-note' WHERE id = :id"
            ),
            {"id": row.id},
        )
    path = Path(__file__).parents[1] / "alembic/versions/0140_clear_legacy_routine_output.py"
    spec = importlib.util.spec_from_file_location("legacy_output_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = await db_session.connection()

    def apply(sync_connection):
        with Operations.context(MigrationContext.configure(sync_connection)):
            migration.upgrade()
            migration.upgrade()  # Idempotent if rollout needs a retry.
            migration.downgrade()

    await connection.run_sync(apply)
    for index, before in enumerate(rows):
        after = await get_routine(db_session, before.id, tenant_id=tenant.id)
        assert after is not None
        for field in ("last_result_tail", "last_error", "delivery_payload", "delivery_note"):
            assert getattr(after, field) == (
                None
                if index == 0
                else {
                    "last_result_tail": "private-tail",
                    "last_error": "private-error",
                    "delivery_payload": "private-payload",
                    "delivery_note": "private-note",
                }[field]
            )
        assert after.enabled == before.enabled
        assert after.next_fire_at == before.next_fire_at
        assert after.updated_at == before.updated_at
