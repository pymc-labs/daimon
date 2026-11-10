"""Migration 0189 copies the workspace settings kept in columns into feature values."""

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _load() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0189_workspace_feature_values.py"
    spec = importlib.util.spec_from_file_location("migration_0189", path)
    assert spec and spec.loader, "the migration module must load"
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def test_upgrade_turns_the_old_columns_into_workspace_values(
    db_session: AsyncSession,
) -> None:
    """A tenant with typed mentions on and a module off keeps both as feature values."""
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    await db_session.execute(
        text("UPDATE tenants SET plain_text_mentions = true WHERE id = :id"), {"id": tenant.id}
    )
    await db_session.execute(
        text(
            "INSERT INTO web_workspace_settings (tenant_id, modules_off, updated_at) "
            "VALUES (:id, ARRAY['credits'], now())"
        ),
        {"id": tenant.id},
    )
    path = Path(__file__).parents[1] / "alembic/versions/0189_workspace_feature_values.py"
    spec = importlib.util.spec_from_file_location("migration_0189", path)
    assert spec and spec.loader, "the migration module must load"
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    await conn.run_sync(upgrade)
    place = FeaturePlace(tenant.id, "web")
    mentions = await feature_in(
        db_session, None, FeaturePlace(tenant.id, "discord"), "plain_text_mentions"
    )
    credits = await feature_in(db_session, None, place, "web.credits")
    untouched = await feature_in(
        db_session, None, FeaturePlace(other.id, "discord"), "plain_text_mentions"
    )
    assert mentions.on is True, "the typed-mention column is now a workspace value"
    assert credits.on is False, "a module that was off stays off"
    assert untouched.on is False, "a tenant that never opted in is unchanged"


async def test_downgrade_restores_only_the_values_still_set(db_session: AsyncSession) -> None:
    """A value reset to Default after the upgrade deleted its row, so the stale column must
    not come back, while one still set is copied back."""
    stale = await make_tenant(db_session)
    live = await make_tenant(db_session)
    for tenant in (stale, live):
        await db_session.execute(
            text("UPDATE tenants SET plain_text_mentions = true WHERE id = :id"), {"id": tenant.id}
        )
        await db_session.execute(
            text(
                "INSERT INTO direct_message_policies (tenant_id, enabled) VALUES (:id, true) "
                "ON CONFLICT (tenant_id) DO UPDATE SET enabled = true"
            ),
            {"id": tenant.id},
        )
        await db_session.execute(
            text(
                "INSERT INTO web_workspace_settings (tenant_id, modules_off, updated_at) "
                "VALUES (:id, ARRAY['credits', 'kept'], now()) "
                "ON CONFLICT (tenant_id) DO UPDATE SET modules_off = excluded.modules_off"
            ),
            {"id": tenant.id},
        )
    migration = _load()
    conn = await db_session.connection()

    def run(sync_conn: Connection, step: str) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(migration, step)()

    await conn.run_sync(run, "upgrade")
    await db_session.execute(
        text("DELETE FROM feature_values WHERE tenant_id = :id AND tier = 'workspace'"),
        {"id": stale.id},
    )
    await conn.run_sync(run, "downgrade")

    async def column(tenant_id: object, sql: str) -> object:
        return await db_session.scalar(text(sql), {"id": tenant_id})

    mentions = "SELECT plain_text_mentions FROM tenants WHERE id = :id"
    dms = "SELECT enabled FROM direct_message_policies WHERE tenant_id = :id"
    modules = "SELECT modules_off FROM web_workspace_settings WHERE tenant_id = :id"
    assert await column(live.id, mentions) is True, "a value still set comes back"
    assert await column(live.id, dms) is True, "direct messages still set come back"
    assert sorted(await column(live.id, modules) or []) == ["credits", "kept"], (
        "modules still off come back and a module this migration never copied is kept"
    )
    assert await column(stale.id, mentions) is False, "a value reset to Default stays reset"
    assert await column(stale.id, dms) is False, "direct messages reset to Default stay off"
    assert await column(stale.id, modules) == ["kept"], "a module back On stays On"
