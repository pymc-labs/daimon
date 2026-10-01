"""Shared fixtures for MCP adapter tests.

MA transport fakes (MARouter, build_fake_anthropic, list_response, sse_response,
send_events_response, json_body) are imported from daimon.testing.ma.

DB fixtures come from daimon.testing.db: one schema per worker, wiped before
every test by `db_clean`. `committing_sessionmaker` adds a factory on the
engine itself (a separate pool connection from `db_session`) for tests that
must observe real COMMIT visibility.
"""

from __future__ import annotations

import pytest_asyncio
from daimon.testing.db import (  # noqa: F401
    db_clean,
    db_engine,
    db_schema,
    db_session,
    db_session_factory,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)


@pytest_asyncio.fixture
async def sessionmaker(
    db_session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> async_sessionmaker[AsyncSession]:
    """Readable alias for `db_session_factory` used throughout MCP tests."""
    return db_session_factory


@pytest_asyncio.fixture
async def committing_sessionmaker(
    db_engine: AsyncEngine,  # noqa: F811
    db_clean: None,  # noqa: F811  # orders this factory after the per-test wipe
) -> async_sessionmaker[AsyncSession]:
    """Sessionmaker on a SEPARATE connection from db_session.

    Use for McpRuntime in mutation tests: data written through this factory
    is only visible from db_session after a real COMMIT. Catches missing
    .begin() bugs that the shared-connection ``sessionmaker`` fixture masks.

    Every `db_engine` connection is pinned to the worker schema, so no
    schema mapping is needed here.
    """
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)
