"""Migration 0190 copies the long-job policy into feature values."""

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.stores.feature_values import feature_revision
from daimon.core.stores.long_jobs import get_policy
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _load() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0190_long_job_features.py"
    spec = importlib.util.spec_from_file_location("migration_0190", path)
    assert spec and spec.loader, "the migration module must load"
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def _policy(
    db: AsyncSession, tenant_id: object, agent_id: str | None, enabled: bool, usd: str, minutes: int
) -> None:
    await db.execute(
        text(
            "INSERT INTO long_job_policy (tenant_id, agent_id, enabled, default_deadline_minutes, "
            "max_deadline_minutes, max_usd, set_by, set_at) "
            "VALUES (:t, :a, :e, 5, :m, :u, 'admin', now())"
        ),
        {"t": tenant_id, "a": agent_id, "e": enabled, "m": minutes, "u": usd},
    )


async def _run(db: AsyncSession, step: str) -> None:
    migration = _load()
    conn = await db.connection()

    def run(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(migration, step)()

    await conn.run_sync(run)


async def test_upgrade_turns_the_policy_into_workspace_and_agent_values(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    untouched = await make_tenant(db_session)
    await _policy(db_session, tenant.id, None, True, "10", 45)
    await _policy(db_session, tenant.id, "off_agent", False, "5", 70)
    await _policy(db_session, tenant.id, "tight", True, "0.5", 5)
    before = await feature_revision(db_session, tenant_id=tenant.id)
    await _run(db_session, "upgrade")

    async def at(agent_id: str | None):
        return await get_policy(
            db_session, place=FeaturePlace(tenant.id, "discord", agent_id=agent_id), settings=None
        )

    workspace = await at(None)
    assert workspace.enabled, "an enabled tenant policy turns long jobs on"
    assert (workspace.max_usd, workspace.max_deadline_minutes) == (10, 30), (
        "a maximum becomes the largest step at or below it"
    )
    assert not (await at("off_agent")).enabled, "an agent policy that is off overrides the tenant"
    tight = await at("tight")
    assert (tight.max_usd, tight.max_deadline_minutes) == (1, 15), "below every step: the smallest"
    assert not (
        await get_policy(db_session, place=FeaturePlace(untouched.id, "discord"), settings=None)
    ).enabled
    assert await feature_revision(db_session, tenant_id=tenant.id) > before
    rows = await db_session.scalar(
        text(
            "SELECT count(*) FROM feature_values WHERE tenant_id = :t AND key = 'long_jobs_max_usd'"
        ),
        {"t": tenant.id},
    )
    assert rows == 2, "the default $5 maximum is not copied"


async def test_downgrade_removes_only_what_the_upgrade_wrote(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _policy(db_session, tenant.id, None, True, "10", 70)
    await _run(db_session, "upgrade")
    await db_session.execute(
        text(
            "INSERT INTO feature_values (tenant_id, tier, agent_id, key, value, surface) "
            "VALUES (:t, 'agent', 'a', 'long_jobs', 'on', 'chat')"
        ),
        {"t": tenant.id},
    )
    await _run(db_session, "downgrade")
    left = (
        await db_session.execute(
            text("SELECT key, surface FROM feature_values WHERE tenant_id = :t"), {"t": tenant.id}
        )
    ).all()
    assert [tuple(row) for row in left] == [("long_jobs", "chat")]
