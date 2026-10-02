"""The channel admin agent standing migration backfills defaults set by server admins."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0043_agent_admin_standing.py"
    spec = importlib.util.spec_from_file_location("migration_channel_admin_agent_standing", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_upgrade_marks_only_defaults_an_admin_account_set(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    admin = await make_account(db_session, tenant=tenant)
    member = await make_account(db_session, tenant=tenant)
    await db_session.execute(
        text("UPDATE accounts SET role = 'admin' WHERE id = :id"), {"id": admin.id}
    )
    for channel, setter in (("c-admin", admin.id), ("c-member", member.id), ("c-none", None)):
        await set_fields(
            db_session,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=channel),
            tenant_id=tenant.id,
            agent_name="a",
            actor_account_id=setter,
        )
    migration = _migration()
    conn = await db_session.connection()

    def roundtrip(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.downgrade()
            migration.upgrade()

    await conn.run_sync(roundtrip)
    rows = await db_session.execute(
        text("SELECT channel_id, agent_name_set_by_admin FROM channel_config")
    )
    assert dict(rows.tuples().all()) == {"c-admin": True, "c-member": False, "c-none": False}, (
        "only a default whose setter is stored as an admin counts as a server admin's"
    )
