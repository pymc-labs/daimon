"""Schema-shape + per-row-derived-tenant tests for migration 0014.

The schema-per-test `db_session` fixture builds the schema via
`Base.metadata.create_all` (not alembic), so the ORM models declared for 0014
must produce the exact columns + named indexes the migration creates. These
tests assert that shape against `information_schema` / `pg_indexes`, and prove
the per-(platform, guild_id) DERIVED tenant identity that the backfill establishes.
"""

from __future__ import annotations

from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.routines import create_routine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_migration_0014_columns_and_index_present(db_session: AsyncSession) -> None:
    schema = (await db_session.execute(text("SELECT current_schema()"))).scalar_one()

    # Migration 0017 flipped usage_events.tenant_id and tenant_user_caps.tenant_id to NOT NULL.
    not_null_tables = ("routines", "payment_events", "usage_events", "tenant_user_caps")
    for table in ("routines", "usage_events", "tenant_user_caps", "payment_events"):
        is_nullable = (
            await db_session.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = :t AND column_name = 'tenant_id'"
                ),
                {"s": schema, "t": table},
            )
        ).scalar_one_or_none()
        if table in not_null_tables:
            assert is_nullable == "NO", f"{table}.tenant_id must exist and be NOT NULL"
        else:
            assert is_nullable == "YES", f"{table}.tenant_id must exist and be NULLABLE"

        fk = (
            await db_session.execute(
                text(
                    "SELECT tc.constraint_name FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "  ON tc.constraint_name = kcu.constraint_name "
                    "  AND tc.table_schema = kcu.table_schema "
                    "WHERE tc.table_schema = :s AND tc.table_name = :t "
                    "  AND tc.constraint_type = 'FOREIGN KEY' AND kcu.column_name = 'tenant_id'"
                ),
                {"s": schema, "t": table},
            )
        ).scalar_one_or_none()
        assert fk is not None, f"{table}.tenant_id must have a FK to tenants.id"

        idx = (
            await db_session.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = :s AND indexname = :i"),
                {"s": schema, "i": f"{table}_tenant_idx"},
            )
        ).scalar_one_or_none()
        assert idx == f"{table}_tenant_idx", f"{table}_tenant_idx FK index must exist"

    # Migration 0023 folded workspaces into tenants. Assert the lifecycle columns
    # (provision_status, archived_at) and unique identity index now live on tenants.
    ps_nullable = (
        await db_session.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'tenants' "
                "AND column_name = 'provision_status'"
            ),
            {"s": schema},
        )
    ).scalar_one_or_none()
    assert ps_nullable == "NO", "tenants.provision_status must exist and be NOT NULL"

    ps_default = (
        await db_session.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'tenants' "
                "AND column_name = 'provision_status'"
            ),
            {"s": schema},
        )
    ).scalar_one()
    assert "ready" in (ps_default or ""), "tenants.provision_status default must be 'ready'"

    archived_nullable = (
        await db_session.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'tenants' "
                "AND column_name = 'archived_at'"
            ),
            {"s": schema},
        )
    ).scalar_one_or_none()
    assert archived_nullable == "YES", "tenants.archived_at must exist and be nullable"

    indexdef = (
        await db_session.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = :s AND indexname = 'uq_tenants_platform_external_id'"
            ),
            {"s": schema},
        )
    ).scalar_one_or_none()
    assert indexdef is not None, "uq_tenants_platform_external_id must exist"
    assert "UNIQUE" in indexdef.upper(), (
        "the (platform, external_id) index on tenants must be UNIQUE"
    )


async def test_migration_0014_backfill_derives_per_row_tenant(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """tenant_deterministic: each guild's routine carries the per-(platform, guild_id)
    DERIVED tenant id — the same property the 0014 backfill establishes per row."""
    result_a = await provision_tenant(db_session_factory, platform="discord", workspace_id="111")
    result_b = await provision_tenant(db_session_factory, platform="discord", workspace_id="222")
    assert result_a.tenant_id != result_b.tenant_id, "two guilds must yield distinct tenants"

    routine_a = await create_routine(
        db_session,
        tenant_id=result_a.tenant_id,
        created_by_user_id="u1",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="*/5 * * * *",
        timezone_="UTC",
        trigger_message="a",
    )
    routine_b = await create_routine(
        db_session,
        tenant_id=result_b.tenant_id,
        created_by_user_id="u2",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="*/5 * * * *",
        timezone_="UTC",
        trigger_message="b",
    )

    assert routine_a.tenant_id == derive_tenant_uuid(platform="discord", workspace_id="111"), (
        "routine for guild 111 must carry derive_tenant_uuid(discord, 111)"
    )
    assert routine_b.tenant_id == derive_tenant_uuid(platform="discord", workspace_id="222"), (
        "routine for guild 222 must carry derive_tenant_uuid(discord, 222)"
    )
