"""Migration 0192 resets saved "Allow writes" approvals choices at every tier."""

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
    path = Path(__file__).parents[1] / "alembic/versions/0192_reset_allow_writes.py"
    spec = importlib.util.spec_from_file_location("migration_0192", path)
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


async def _set(
    db: AsyncSession, tenant: object, tier: str, key: str, value: str, *, thread: str = ""
) -> None:
    await db.execute(
        text(
            "INSERT INTO feature_values (tenant_id, tier, agent_id, thread_key, key, value, surface) "
            "VALUES (:t, :tier, :a, :th, :k, :v, 'chat')"
        ),
        {
            "t": tenant,
            "tier": tier,
            "a": "a1" if tier in ("agent", "thread") else "",
            "th": thread,
            "k": key,
            "v": value,
        },
    )


async def test_upgrade_deletes_allow_writes_at_every_tier_and_keeps_the_rest(
    db_session: AsyncSession,
) -> None:
    """Only approvals rows set to Allow writes go; Ask rows, other keys and other tenants stay."""
    tenant, quiet = await make_tenant(db_session), await make_tenant(db_session)
    await _set(db_session, tenant.id, "workspace", "approvals", "off")
    await _set(db_session, tenant.id, "thread", "approvals", "off", thread="t1")
    await _set(db_session, tenant.id, "agent", "approvals", "on")
    await _set(db_session, tenant.id, "workspace", "guidance", "off")
    await _set(db_session, quiet.id, "workspace", "approvals", "on")
    before = await feature_revision(db_session, tenant_id=tenant.id)
    quiet_before = await feature_revision(db_session, tenant_id=quiet.id)
    await _run(db_session, "upgrade")
    left = (
        await db_session.execute(
            text("SELECT tier, key, value FROM feature_values WHERE tenant_id = :t ORDER BY tier"),
            {"t": tenant.id},
        )
    ).all()
    assert [tuple(r) for r in left] == [
        ("agent", "approvals", "on"),
        ("workspace", "guidance", "off"),
    ], "only the Allow writes rows are deleted"
    assert await feature_revision(db_session, tenant_id=tenant.id) > before, "caches refresh"
    assert await feature_revision(db_session, tenant_id=quiet.id) == quiet_before, (
        "a tenant with no Allow writes rows is untouched"
    )
