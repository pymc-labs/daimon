"""Shared fixtures for MCP adapter tests.

MA transport fakes (MARouter, build_fake_anthropic, list_response, sse_response,
send_events_response, json_body) are imported from daimon.testing.ma.

DB engine is imported from daimon.testing.db. db_session stays local because
MCP's version takes `test_schema` (a separate fixture) rather than generating
the schema name inline — both db_session and committing_sessionmaker must share
the same schema name, so test_schema mediates that.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from daimon.core._models import Base
from daimon.testing.db import db_engine  # noqa: F401
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)


@pytest_asyncio.fixture
async def test_schema() -> str:
    """Generate a unique per-test schema name shared by db_session and committing_sessionmaker."""
    return f"test_{uuid.uuid4().hex}"


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine, test_schema: str) -> AsyncIterator[AsyncSession]:  # noqa: F811
    schema = test_schema
    async with db_engine.connect() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}", public'))
        conn = await conn.execution_options(schema_translate_map={None: schema})
        await conn.run_sync(Base.metadata.create_all)
        await conn.commit()

        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await conn.rollback()
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await conn.commit()


@pytest_asyncio.fixture
async def db_session_factory(
    db_session: AsyncSession,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=db_session.bind, expire_on_commit=False)


@pytest_asyncio.fixture
async def sessionmaker(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Readable alias for `db_session_factory` used throughout MCP tests."""
    return db_session_factory


@pytest_asyncio.fixture
async def committing_sessionmaker(
    db_engine: AsyncEngine,  # noqa: F811
    test_schema: str,
    db_session: AsyncSession,  # noqa: ARG001  # ensures schema is created before this factory connects
) -> async_sessionmaker[AsyncSession]:
    """Sessionmaker on a SEPARATE connection from db_session.

    Use for McpRuntime in mutation tests: data written through this factory
    is only visible from db_session after a real COMMIT. Catches missing
    .begin() bugs that the shared-connection ``sessionmaker`` fixture masks.

    Depends on ``db_session`` to guarantee the ephemeral test schema exists
    before any session from this factory tries to connect to it.
    """
    return async_sessionmaker(
        bind=db_engine.execution_options(schema_translate_map={None: test_schema}),
        expire_on_commit=False,
    )
