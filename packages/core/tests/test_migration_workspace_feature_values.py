"""Migration 0188 copies the workspace settings kept in columns into feature values."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


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
    path = Path(__file__).parents[1] / "alembic/versions/0188_workspace_feature_values.py"
    spec = importlib.util.spec_from_file_location("migration_0188", path)
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
