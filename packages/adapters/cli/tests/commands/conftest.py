"""Shared fixtures for CLI `commands` tests.

`schema_sessionmaker` is used by tests that invoke the real Typer CLI via
`CliRunner` — each command owns its own `asyncio.run` loop, so the factory
sits on the NullPool twin of the worker engine: asyncpg connections are
bound to the loop that opened them, and a pooled connection (or the single
shared connection behind `db_session`) would break across separate
`asyncio.run` calls. It depends on `db_clean` so the worker schema is wiped
before the command runs.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def schema_sessionmaker(
    db_nullpool_engine: AsyncEngine,
    db_clean: None,  # orders the factory after the per-test wipe
) -> async_sessionmaker[AsyncSession]:
    """NullPool sessionmaker on the wiped worker schema."""
    return async_sessionmaker(db_nullpool_engine, expire_on_commit=False, class_=AsyncSession)
