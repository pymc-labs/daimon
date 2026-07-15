"""Smoke tests for daimon.testing.db module.

These tests require an external Postgres instance with a migrated daimon_test
database. Set DAIMON_DATABASE__TEST_URL=postgresql+asyncpg://... before running.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_db_engine_creates_engine(db_engine: AsyncEngine) -> None:
    """db_engine fixture provides an AsyncEngine instance bound to the test DB."""
    assert isinstance(db_engine, AsyncEngine), (
        "db_engine fixture should provide an AsyncEngine instance"
    )


@pytest.mark.asyncio
async def test_db_session_provides_isolation(db_session: AsyncSession) -> None:
    """db_session is an AsyncSession pinned to a per-test schema starting with 'test_'."""
    assert isinstance(db_session, AsyncSession), (
        "db_session fixture should provide an AsyncSession instance"
    )
    # Verify schema-per-test isolation: current_schema() should start with 'test_'
    result = await db_session.execute(text("SELECT current_schema()"))
    schema_name: str = result.scalar_one()
    assert schema_name.startswith("test_"), (
        f"db_session should be pinned to a per-test schema (got: {schema_name!r})"
    )


@pytest.mark.asyncio
async def test_db_session_factory_returns_sessionmaker(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """db_session_factory fixture provides an async_sessionmaker instance."""
    assert isinstance(db_session_factory, async_sessionmaker), (
        "db_session_factory fixture should provide an async_sessionmaker instance"
    )
