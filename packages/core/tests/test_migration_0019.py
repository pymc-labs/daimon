"""Schema-shape test for migration 0019 (Phase 58.3).

The db_session fixture guarantees alembic upgrade head has been applied and
the schema-per-test isolation is in place. Asserts: tenant_config exists with
pk/fk/check constraints; workspace_config and tenant_system_config are dropped;
channel_config PK is (tenant_id, channel_id) with FK to tenants, not workspaces;
platform/workspace_id columns are gone from channel_config.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_migration_0019_schema_shape(db_session: AsyncSession) -> None:
    """After migration 0019: tenant_config exists, old tables dropped, channel_config re-keyed."""
    schema = (await db_session.execute(text("SELECT current_schema()"))).scalar_one()

    # workspace_config and tenant_system_config must be gone
    gone = (
        await db_session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :s "
                "AND table_name IN ('workspace_config', 'tenant_system_config')"
            ),
            {"s": schema},
        )
    ).fetchall()
    assert gone == [], "workspace_config and tenant_system_config must be dropped by migration 0019"

    # tenant_config must exist
    tenant_config_exists = (
        await db_session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :s AND table_name = 'tenant_config'"
            ),
            {"s": schema},
        )
    ).scalar_one_or_none()
    assert tenant_config_exists == "tenant_config", "tenant_config must exist after migration 0019"

    # Named constraints must exist on tenant_config
    # Use a subquery for the schema OID — ":s::regnamespace" conflicts with
    # asyncpg's parameter binding because "::" is interpreted as part of ":s".
    for constraint in ("pk_tenant_config", "fk_tenant_config_tenants", "ck_tenant_config_mode"):
        name = (
            await db_session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conname = :n "
                    "AND connamespace = (SELECT oid FROM pg_namespace WHERE nspname = :s)"
                ),
                {"n": constraint, "s": schema},
            )
        ).scalar_one_or_none()
        assert name == constraint, (
            f"constraint {constraint!r} must exist on tenant_config after migration 0019"
        )

    # channel_config must NOT have platform or workspace_id columns
    stale_cols = (
        await db_session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'channel_config' "
                "AND column_name IN ('platform', 'workspace_id')"
            ),
            {"s": schema},
        )
    ).fetchall()
    assert stale_cols == [], (
        "channel_config must not have platform or workspace_id columns after migration 0019"
    )

    # channel_config FK must point to tenants (not workspaces)
    ch_fk = (
        await db_session.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conname = 'fk_channel_config_tenants' "
                "AND connamespace = (SELECT oid FROM pg_namespace WHERE nspname = :s)"
            ),
            {"s": schema},
        )
    ).scalar_one_or_none()
    assert ch_fk == "fk_channel_config_tenants", (
        "channel_config must FK to tenants (fk_channel_config_tenants) after migration 0019"
    )

    # channel_config PK must have exactly 2 columns (tenant_id, channel_id)
    pk_key_count = (
        await db_session.execute(
            text(
                "SELECT array_length(conkey, 1) FROM pg_constraint "
                "WHERE conname = 'pk_channel_config' "
                "AND connamespace = (SELECT oid FROM pg_namespace WHERE nspname = :s)"
            ),
            {"s": schema},
        )
    ).scalar_one_or_none()
    assert pk_key_count == 2, (
        "channel_config PK must have exactly 2 columns (tenant_id, channel_id) "
        f"after migration 0019; got {pk_key_count!r}"
    )
