"""Existing live sessions keep their responder when thread bindings are introduced."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0048_opened_thread_bindings.py"
    spec = importlib.util.spec_from_file_location("migration_opened_threads", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_backfill_binds_live_threads_to_recorded_agent(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    migration = _migration()
    conn = await db_session.connection()

    def downgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.downgrade()

    await conn.run_sync(downgrade)
    for thread_id, status, name in (
        ("live-root", "live", "first"),
        ("dead-root", "dead", "former"),
    ):
        await db_session.execute(
            text(
                "INSERT INTO thread_sessions "
                "(tenant_id, platform, thread_id, channel_id, ma_session_id, ma_agent_id, "
                "effective_config, status) VALUES "
                "(:tenant, 'slack', :thread, 'channel', :session, :agent, "
                "CAST(:config AS jsonb), :status)"
            ),
            {
                "tenant": tenant.id,
                "thread": thread_id,
                "session": f"session-{thread_id}",
                "agent": f"agent-{name}",
                "config": json.dumps({"agent_name": name}),
                "status": status,
            },
        )

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    await conn.run_sync(upgrade)
    rows = (
        (
            await db_session.execute(
                text(
                    "SELECT thread_id, kind, responder_ma_agent_id, responder_name "
                    "FROM thread_agent_bindings ORDER BY thread_id"
                )
            )
        )
        .tuples()
        .all()
    )
    assert rows == [("live-root", "opened", "agent-first", "first")]
