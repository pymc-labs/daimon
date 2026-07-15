"""Shape-assertion tests for migration 0023.

After `alembic upgrade head`, verifies:
  - tenants table has all 5 folded columns with correct nullability
  - UNIQUE constraint uq_tenants_platform_external_id exists on tenants
  - workspaces table no longer exists
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_migration_0023_tenants_columns(db_engine: AsyncEngine) -> None:
    """tenants table carries the 5 folded columns with expected nullability after 0023."""
    async with db_engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT column_name, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'tenants' "
                "AND column_name IN "
                "('platform', 'external_id', 'provision_status', 'archived_at', 'registered_at') "
                "ORDER BY column_name"
            )
        )
        rows = {row[0]: row[1] for row in result.fetchall()}

    assert set(rows.keys()) == {
        "platform",
        "external_id",
        "provision_status",
        "archived_at",
        "registered_at",
    }, "all 5 folded columns must exist on tenants after migration 0023"

    assert rows["platform"] == "NO", "tenants.platform must be NOT NULL"
    assert rows["external_id"] == "NO", "tenants.external_id must be NOT NULL"
    assert rows["provision_status"] == "NO", "tenants.provision_status must be NOT NULL"
    assert rows["registered_at"] == "NO", "tenants.registered_at must be NOT NULL"
    assert rows["archived_at"] == "YES", "tenants.archived_at must be nullable"


@pytest.mark.asyncio
async def test_migration_0023_unique_constraint(db_engine: AsyncEngine) -> None:
    """UNIQUE constraint uq_tenants_platform_external_id exists on tenants after 0023."""
    async with db_engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT conname FROM pg_constraint "
                "WHERE conname = 'uq_tenants_platform_external_id' "
                "AND conrelid = 'tenants'::regclass "
                "AND contype = 'u'"
            )
        )
        rows = result.fetchall()

    assert len(rows) == 1, (
        "UNIQUE constraint uq_tenants_platform_external_id must exist on tenants after 0023"
    )


@pytest.mark.asyncio
async def test_migration_0023_workspaces_dropped(db_engine: AsyncEngine) -> None:
    """workspaces table must not exist after migration 0023 drops it."""
    async with db_engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'workspaces'"
            )
        )
        rows = result.fetchall()

    assert len(rows) == 0, "workspaces table must not exist after migration 0023"
