"""Migration 0125 turns plain-text mentions on for new tenants and leaves existing ones."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.stores.tenants import get_tenant, set_plain_text_mentions
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> object:
    path = Path(__file__).parents[1] / "alembic/versions/0125_plain_mentions_default_on.py"
    spec = importlib.util.spec_from_file_location("migration_plain_mentions_default_on", path)
    assert spec and spec.loader, "the migration module must load"
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.mark.fresh_schema
async def test_new_tenants_start_on_and_existing_tenants_keep_their_value(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(
        text("ALTER TABLE tenants ALTER COLUMN plain_text_mentions SET DEFAULT false")
    )
    untouched = await make_tenant(db_session, workspace_id="before-untouched")
    chose_on = await make_tenant(db_session, workspace_id="before-on")
    await set_plain_text_mentions(db_session, tenant_id=chose_on.id, enabled=True)
    migration = _migration()
    conn = await db_session.connection()

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()  # type: ignore[attr-defined]

    await conn.run_sync(upgrade)
    created_after = await make_tenant(db_session, workspace_id="after")
    db_session.expire_all()

    before = await get_tenant(db_session, untouched.id)
    assert before is not None and before.plain_text_mentions is False, (
        "nothing records whether an admin chose off, so an existing tenant keeps its value"
    )
    on = await get_tenant(db_session, chose_on.id)
    assert on is not None and on.plain_text_mentions is True
    after = await get_tenant(db_session, created_after.id)
    assert after is not None and after.plain_text_mentions is True, (
        "a tenant created after the upgrade starts with plain-text mentions on"
    )

    def downgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.downgrade()  # type: ignore[attr-defined]

    await conn.run_sync(downgrade)
    created_later = await make_tenant(db_session, workspace_id="after-downgrade")
    db_session.expire_all()
    later = await get_tenant(db_session, created_later.id)
    assert later is not None and later.plain_text_mentions is False
