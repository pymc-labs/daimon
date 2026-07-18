"""Round-trip test for Alembic 0009.

Verifies that after `alembic upgrade head`, both tables created by 0009 exist
with the correct composite PK constraint names. The db_engine fixture guarantees
migrations have been applied before any test runs.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_migration_0009_creates_both_tables(db_engine: AsyncEngine) -> None:
    """After upgrade head, agent_files and agent_repo_binding exist with the expected PKs."""
    async with db_engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name IN ('agent_files', 'agent_repo_binding') "
                "ORDER BY table_name"
            )
        )
        names = [row[0] for row in result.fetchall()]
    assert names == ["agent_files", "agent_repo_binding"], (
        "both tables should exist after migrations run"
    )


@pytest.mark.asyncio
async def test_migration_0009_pk_constraint_names(db_engine: AsyncEngine) -> None:
    """Composite PK constraint names match ORM __table_args__ exactly."""
    async with db_engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT conname FROM pg_constraint "
                "WHERE conname IN ('pk_agent_files', 'pk_agent_repo_binding') "
                "AND connamespace = 'public'::regnamespace "
                "ORDER BY conname"
            )
        )
        names = [row[0] for row in result.fetchall()]
    assert names == ["pk_agent_files", "pk_agent_repo_binding"], (
        "ORM and migration must agree on PK constraint names"
    )
