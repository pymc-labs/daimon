"""DB fixtures for Daimon test suites.

Provides session-scoped engine and per-test schema isolation fixtures.
Import these into a package conftest to make them discoverable by pytest:

    # tests/conftest.py
    from daimon.testing.db import db_engine, db_session, db_session_factory  # noqa: F401

Strategy:
- One external Postgres (the compose service).
- One dedicated test database (`daimon_test`). Checked by a substring guard;
  misconfiguration fails loudly instead of nuking dev data.
- Session-scoped `db_engine` points at `daimon_test` and verifies migrations
  have been applied (the `alembic_version` row exists).
- Per-test `db_session` creates a fresh schema (`test_<uuid>`), sets
  `search_path` to it, issues `Base.metadata.create_all` into that schema
  (fast — pure DDL, no alembic), yields, then `DROP SCHEMA ... CASCADE`.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import pytest_asyncio
from daimon.core._models import Base
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _require_test_dsn() -> str:
    """Read the test DSN and refuse to proceed unless it contains 'test'."""
    url = os.environ.get("DAIMON_DATABASE__TEST_URL") or os.environ.get("DAIMON_TEST_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DAIMON_DATABASE__TEST_URL (or legacy DAIMON_TEST_DATABASE_URL) must be set "
            "to run daimon tests."
        )
    db_name = urlparse(url).path.lstrip("/")
    if "test" not in db_name:
        raise RuntimeError(
            f"Refusing to run destructive fixtures against database {db_name!r} "
            f"(from {url!r}) — database name must contain the substring 'test'."
        )
    return url


@pytest_asyncio.fixture(scope="session")
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Session-scoped async engine bound to the test DB.

    Assumes `alembic upgrade head` has already been applied to the test DB (CI
    runs this explicitly; local dev runs `uv run alembic upgrade head` once).
    Raises if the migration baseline is missing.
    """
    url = _require_test_dsn()
    engine = create_async_engine(url, pool_size=2, max_overflow=0, pool_timeout=30)

    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT to_regclass('public.alembic_version') IS NOT NULL AS has_alembic")
        )
        has_alembic = result.scalar_one()
        if not has_alembic:
            await engine.dispose()
            raise RuntimeError(
                "alembic_version table is missing on the test DB. Run "
                "`uv run alembic upgrade head` from the repo root against "
                "DAIMON_DATABASE_URL=<test DSN> before invoking pytest."
            )

    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Per-test AsyncSession pinned to a fresh `test_<uuid>` schema.

    Flow (all over a single connection that outlives the session):
      1. CREATE SCHEMA test_<uuid>
      2. SET search_path TO test_<uuid>
      3. Base.metadata.create_all — DDL lands in the scoped schema since
         none of our Table objects carry an explicit `schema=` argument.
      4. Yield an AsyncSession bound to that connection.
      5. DROP SCHEMA test_<uuid> CASCADE.
    """
    schema = f"test_{uuid.uuid4().hex}"
    async with db_engine.connect() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}", public'))
        mapped_conn = await conn.execution_options(schema_translate_map={None: schema})
        await mapped_conn.run_sync(Base.metadata.create_all)
        await mapped_conn.commit()

        session = AsyncSession(bind=mapped_conn, expire_on_commit=False)
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
    """Session factory bound to the same connection as `db_session`.

    The orchestrator opens fresh `async with session_factory() as s, s.begin():`
    transactions per resource. Binding to `db_session.bind` (the underlying
    connection) means factory-created sessions share the same per-test schema
    (`test_<uuid>`) via `search_path`, and their commits land on that connection
    — visible to subsequent factory sessions within the same test.
    """
    return async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
