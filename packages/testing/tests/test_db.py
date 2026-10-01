"""Tests for daimon.testing.db: the per-worker schema fixture graph.

These tests require an external Postgres instance with a migrated daimon_test
database. Set DAIMON_DATABASE__TEST_URL=postgresql+asyncpg://... before running.
"""

from __future__ import annotations

import asyncio
import os
import re

import pytest
from daimon.core._models import Base
from daimon.testing.db import (
    _require_test_dsn,  # pyright: ignore[reportPrivateUsage]
    build_test_engine,
    sweep_orphan_schemas,
    truncate_all,
)
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

# A pid past this host's pid_max (4194304 on Linux) can never be alive.
_DEAD_PID = 4194304


@pytest.mark.asyncio
async def test_db_engine_creates_engine(db_engine: AsyncEngine) -> None:
    """db_engine fixture provides an AsyncEngine instance bound to the test DB."""
    assert isinstance(db_engine, AsyncEngine), (
        "db_engine fixture should provide an AsyncEngine instance"
    )


@pytest.mark.asyncio
async def test_db_session_provides_isolation(db_session: AsyncSession) -> None:
    """db_session is an AsyncSession pinned to the worker's schema, named test_*."""
    assert isinstance(db_session, AsyncSession), (
        "db_session fixture should provide an AsyncSession instance"
    )
    # Verify the worker schema is in effect: current_schema() should start with 'test_'
    result = await db_session.execute(text("SELECT current_schema()"))
    schema_name: str = result.scalar_one()
    assert schema_name.startswith("test_"), (
        f"db_session should be pinned to a worker test schema (got: {schema_name!r})"
    )


@pytest.mark.asyncio
async def test_db_session_factory_returns_sessionmaker(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """db_session_factory fixture provides an async_sessionmaker instance."""
    assert isinstance(db_session_factory, async_sessionmaker), (
        "db_session_factory fixture should provide an async_sessionmaker instance"
    )


def test_db_schema_names_this_worker_process_when_requested(db_schema: str) -> None:
    match = re.match(r"^test_w(\d+)_[0-9a-f]{8}$", db_schema)
    assert match is not None, f"worker schema must be test_w<pid>_<8hex>, got {db_schema!r}"
    assert int(match.group(1)) == os.getpid(), "worker schema must carry this process's pid"


async def test_truncate_all_removes_committed_rows_when_called(
    db_engine: AsyncEngine, db_schema: str, db_session: AsyncSession
) -> None:
    await make_tenant(db_session)
    await db_session.commit()

    async with db_engine.begin() as conn:
        await truncate_all(conn, db_schema)

    async with db_engine.connect() as fresh:
        count = (
            await fresh.execute(text(f'SELECT count(*) FROM "{db_schema}".tenants'))
        ).scalar_one()
    assert count == 0, "a fresh connection must see no rows after truncate_all"


@pytest.mark.fresh_schema
async def test_db_session_uses_private_schema_when_marked_fresh_schema(
    db_session: AsyncSession, db_schema: str
) -> None:
    schema = (await db_session.execute(text("SELECT current_schema()"))).scalar_one()
    assert schema.startswith("test_f"), f"fresh_schema session must sit on test_f*, got {schema!r}"
    assert schema != db_schema, "fresh_schema must not reuse the shared worker schema"

    tables = (
        await db_session.execute(
            text("SELECT count(*) FROM pg_tables WHERE schemaname = :s"), {"s": schema}
        )
    ).scalar_one()
    assert tables == len(Base.metadata.tables), "fresh schema must carry every ORM table"


async def test_other_connections_see_worker_schema_and_committed_rows_when_pinned(
    db_engine: AsyncEngine, db_schema: str, db_session: AsyncSession
) -> None:
    """Covers `committing_sessionmaker` (second pool connection) and the CLI NullPool path."""
    tenant = await make_tenant(db_session)
    await db_session.commit()

    async with db_engine.connect() as pooled:
        assert (await pooled.execute(text("SELECT current_schema()"))).scalar_one() == db_schema
        seen = (
            await pooled.execute(
                text("SELECT count(*) FROM tenants WHERE id = :id"), {"id": tenant.id}
            )
        ).scalar_one()
        assert seen == 1, "a second pooled connection must see rows committed via db_session"

    nullpool = build_test_engine(
        os.environ["DAIMON_DATABASE__TEST_URL"], db_schema, poolclass=NullPool
    )
    try:
        async with nullpool.connect() as conn:
            assert (await conn.execute(text("SELECT current_schema()"))).scalar_one() == db_schema
            seen = (
                await conn.execute(
                    text("SELECT count(*) FROM tenants WHERE id = :id"), {"id": tenant.id}
                )
            ).scalar_one()
            assert seen == 1, "a NullPool connection pinned to the schema must see committed rows"
    finally:
        await nullpool.dispose()


def test_require_test_dsn_refuses_database_without_test_in_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DAIMON_DATABASE__TEST_URL", "postgresql+asyncpg://u:p@localhost:5432/daimon"
    )
    with pytest.raises(RuntimeError, match="must contain the substring 'test'"):
        _require_test_dsn()


def test_require_test_dsn_refuses_unset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAIMON_DATABASE__TEST_URL", raising=False)
    with pytest.raises(RuntimeError, match="must be set"):
        _require_test_dsn()


async def test_truncate_all_raises_when_another_connection_holds_a_row_lock(
    db_engine: AsyncEngine, db_schema: str, db_session: AsyncSession
) -> None:
    await make_tenant(db_session)
    await db_session.commit()
    # db_session's open transaction now holds a row lock the wipe must wait on.
    await db_session.execute(text("SELECT * FROM tenants FOR UPDATE"))

    try:
        with pytest.raises(RuntimeError, match="timed out"):
            async with db_engine.begin() as conn:
                await truncate_all(conn, db_schema, lock_timeout="200ms")
    finally:
        await db_session.rollback()


async def test_sweep_orphan_schemas_drops_dead_pid_and_keeps_live_pid(
    db_engine: AsyncEngine,
) -> None:
    live = f"test_w{os.getpid()}_deadbeef"
    dead = f"test_w{_DEAD_PID}_deadbeef"
    async with db_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{live}"'))
        await conn.execute(text(f'CREATE SCHEMA "{dead}"'))

    async def _remaining() -> list[str]:
        async with db_engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT nspname FROM pg_namespace WHERE nspname IN (:live, :dead)"),
                {"live": live, "dead": dead},
            )
            return list(rows.scalars().all())

    try:
        # Another worker's engine setup may hold the sweep lock (in which case
        # this call returns [] and we retry) or may itself sweep the dead schema
        # before this call sees it. Either way the observable outcome is the
        # same: the dead schema is gone and the live one survives.
        remaining = await _remaining()
        for _ in range(200):
            if dead not in remaining:
                break
            dropped = await sweep_orphan_schemas(db_engine, limit=10_000)
            assert live not in dropped, "schema of a live pid must survive the sweep"
            remaining = await _remaining()
            if dead in remaining:
                await asyncio.sleep(0.1)

        assert remaining == [live], (
            f"the dead-pid schema must be swept and the live one kept, got {remaining!r}"
        )
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{live}" CASCADE'))
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{dead}" CASCADE'))
