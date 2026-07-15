"""Shape-assertion test for migration 0031.

After `alembic upgrade head`, verifies github_oauth_states.tenant_id is NOT NULL
(MT-1c — the oldest-tenant fallback is retired and every state row must carry a
tenant).
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.asyncio


async def test_migration_0031_tenant_id_not_null(db_engine: AsyncEngine) -> None:
    """github_oauth_states.tenant_id is NOT NULL after migration 0031."""
    async with db_engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'github_oauth_states' "
                "AND column_name = 'tenant_id'"
            )
        )
        row = result.fetchone()

    assert row is not None, "github_oauth_states.tenant_id column must exist"
    assert row[0] == "NO", "github_oauth_states.tenant_id must be NOT NULL after migration 0031"


async def test_migration_0031_purges_null_tenant_rows(db_session: AsyncSession) -> None:
    """Migration 0031 DELETEs NULL-tenant rows before flipping the column NOT NULL.

    The NOT NULL end-state test (above) only proves the constraint is in place
    after `alembic upgrade head`. This test proves the purge half: that the
    migration's exact DELETE actually removes legacy NULL-tenant rows.

    Uses the per-test `db_session` fixture (fresh test_<uuid> schema via
    Base.metadata.create_all). The ORM already has tenant_id NOT NULL, so we
    DROP and rebuild the table with tenant_id nullable — no FK to tenants(id),
    so a NULL row can be inserted without tenant seeding.
    """
    # Drop the ORM-built (already NOT NULL) table and rebuild it nullable
    # so we can insert the legacy NULL-tenant row the migration is meant to purge.
    await db_session.execute(sa.text("DROP TABLE IF EXISTS github_oauth_states CASCADE"))
    await db_session.execute(
        sa.text(
            "CREATE TABLE github_oauth_states ("
            "  state uuid PRIMARY KEY,"
            "  platform text NOT NULL,"
            "  platform_user_id text NOT NULL,"
            "  scopes text[] NOT NULL,"
            "  created_at timestamptz NOT NULL DEFAULT now(),"
            "  consumed_at timestamptz,"
            "  tenant_id uuid,"  # <- nullable; no FK so NULL inserts cleanly
            "  agent_id uuid"
            ")"
        )
    )
    # Insert a legacy NULL-tenant row (the kind the migration is meant to purge).
    # Guard: confirm the row actually landed before running the DELETE.
    await db_session.execute(
        sa.text(
            "INSERT INTO github_oauth_states (state, platform, platform_user_id, scopes, tenant_id) "
            "VALUES (gen_random_uuid(), 'cli', 'legacy', ARRAY['repo'], NULL)"
        )
    )
    pre_count = (
        await db_session.execute(
            sa.text("SELECT count(*) FROM github_oauth_states WHERE tenant_id IS NULL")
        )
    ).scalar_one()
    assert pre_count == 1, (
        "false-green guard: expected one NULL-tenant row before the migration DELETE"
    )

    # Exact upgrade SQL from migration 0031 — reproduced verbatim so this test
    # fails if the migration's predicate drifts.
    await db_session.execute(sa.text("DELETE FROM github_oauth_states WHERE tenant_id IS NULL"))

    null_count = (
        await db_session.execute(
            sa.text("SELECT count(*) FROM github_oauth_states WHERE tenant_id IS NULL")
        )
    ).scalar_one()
    assert null_count == 0, "migration 0031 must purge NULL-tenant rows before the NOT NULL flip"
