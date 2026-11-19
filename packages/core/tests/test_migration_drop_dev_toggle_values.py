"""Migration 0191 deletes the stored values of the nine retired development features."""

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.stores.feature_values import feature_revision
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _load() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0191_drop_dev_toggle_values.py"
    spec = importlib.util.spec_from_file_location("migration_0191", path)
    assert spec and spec.loader, "the migration module must load"
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def _run(db: AsyncSession, step: str) -> None:
    migration = _load()
    conn = await db.connection()

    def run(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(migration, step)()

    await conn.run_sync(run)


async def _set(db: AsyncSession, tenant: object, tier: str, agent: str, key: str) -> None:
    await db.execute(
        text(
            "INSERT INTO feature_values (tenant_id, tier, agent_id, key, value, surface) "
            "VALUES (:t, :tier, :a, :k, 'off', 'chat')"
        ),
        {"t": tenant, "tier": tier, "a": agent, "k": key},
    )


async def test_upgrade_drops_retired_keys_and_keeps_live_ones(db_session: AsyncSession) -> None:
    """Only the nine retired keys go; other rows and other tenants' revisions stay."""
    tenant, quiet = await make_tenant(db_session), await make_tenant(db_session)
    for key in ("daimon_tools", "turn_controls", "waiting_card", "status_log"):
        await _set(db_session, tenant.id, "workspace", "", key)
    await _set(db_session, tenant.id, "agent", "a1", "checkpoint_prompt")
    await _set(db_session, tenant.id, "workspace", "", "guidance")
    before = await feature_revision(db_session, tenant_id=tenant.id)
    quiet_before = await feature_revision(db_session, tenant_id=quiet.id)
    await _run(db_session, "upgrade")
    left = (
        await db_session.scalars(
            text("SELECT key FROM feature_values WHERE tenant_id = :t"), {"t": tenant.id}
        )
    ).all()
    assert list(left) == ["guidance"], "only the live feature keeps its row"
    assert await feature_revision(db_session, tenant_id=tenant.id) > before, "caches refresh"
    assert await feature_revision(db_session, tenant_id=quiet.id) == quiet_before, (
        "a tenant with no retired rows is untouched"
    )
