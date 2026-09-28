"""DB fixtures for daimon test suites.

Import these into a package conftest to make them discoverable by pytest:

    # tests/conftest.py
    from daimon.testing.db import (  # noqa: F401
        db_clean,
        db_engine,
        db_nullpool_engine,
        db_schema,
        db_session,
        db_session_factory,
    )

Model:

- One external Postgres (the compose service) and one dedicated test database
  whose name contains ``test`` (substring guard; misconfiguration fails loudly
  instead of nuking dev data). Migrations must already be applied there.
- Each pytest worker process owns ONE schema, ``test_w<pid>_<8hex>``, built once
  per session (``CREATE SCHEMA`` + ``Base.metadata.create_all``) and dropped at
  session end. The pid + nonce keep two worktrees' ``gw0`` workers apart on the
  shared database.
- Every connection the worker engine opens carries ``search_path = <schema>,
  public`` and ``application_name = <schema>`` as asyncpg *server settings*
  (startup-packet defaults, so they survive pool reuse and ``RESET``). Nothing
  ever issues ``SET search_path`` on a ``db_engine`` connection.
- Per test, ``db_clean`` wipes every ORM table in the worker schema at SETUP
  (one DO block of child-first DELETEs, a few ms). ``public`` is never touched:
  other worktrees share it, and a few own-engine tests write there on purpose.
- ``@pytest.mark.fresh_schema`` opts a test out of the shared schema: it gets a
  private ``test_f<pid>_<8hex>`` schema on its own NullPool engine
  (CREATE / create_all / DROP around the test). Use it for tests that run DDL.
- A worker that dies by SIGKILL leaves its schema behind; ``sweep_orphan_schemas``
  reclaims a bounded number of such orphans at engine setup.
- Schema DDL runs in small transactions. One ``DROP SCHEMA ... CASCADE`` over
  the 45 ORM tables holds ~970 locks and ``create_all`` ~380; the server's lock
  table (``max_locks_per_transaction`` × ``max_connections``, 6,400 by default)
  fits about six such drops, and 16 workers finishing together used to fail
  with "out of shared memory". Tables are therefore created in chunks and
  dropped one per transaction.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Literal, cast
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from daimon.core._models import Base
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, Pool

#: Names this module creates: ``test_w<pid>_<8hex>`` (worker) / ``test_f<pid>_<8hex>`` (fresh).
WORKER_SCHEMA_PATTERN = re.compile(r"^test_[wf](\d+)_[0-9a-f]{8}$")
_IDENTIFIER_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")
_INTERVAL_PATTERN = re.compile(r"^\d+(ms|s|min)?$")
_SWEEP_LOCK_KEY = "daimon:test-schema-sweep"
_LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
_CREATE_CHUNK_SIZE = 8
_DROP_CHUNK_SIZE = 4

# nodeid of the last test that used the DB in this worker; named in the error
# when the next test's wipe times out on a lock that test left behind.
_last_db_test: str | None = None
_TEST_CRYPTO_KEYS = (Fernet.generate_key().decode("ascii"),)


def _require_test_dsn() -> str:
    """Read the test DSN and refuse to proceed unless it contains 'test'."""
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        raise RuntimeError("DAIMON_DATABASE__TEST_URL must be set to run daimon tests.")
    db_name = urlparse(url).path.lstrip("/")
    if "test" not in db_name:
        raise RuntimeError(
            f"Refusing to run destructive fixtures against database {db_name!r} "
            f"(from {url!r}) — database name must contain the substring 'test'."
        )
    return url


def _check_identifier(schema: str) -> None:
    if not _IDENTIFIER_PATTERN.match(schema):
        raise ValueError(f"unsafe schema name {schema!r}")


def _new_schema_name(kind: Literal["w", "f"]) -> str:
    return f"test_{kind}{os.getpid()}_{secrets.token_hex(4)}"


def _request_item(request: pytest.FixtureRequest) -> pytest.Item:
    """The test item behind a function-scoped request."""
    return cast(pytest.Item, request.node)  # pyright: ignore[reportUnknownMemberType]  # pytest leaves FixtureRequest.node unannotated


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def build_test_engine(
    url: str,
    schema: str,
    *,
    poolclass: type[Pool] | None = None,
    **engine_kwargs: object,
) -> AsyncEngine:
    """Build an engine whose every connection is pinned to ``schema``.

    ``search_path`` and ``application_name`` ride in asyncpg's startup packet,
    so they hold for the connection's whole life (pool reuse, ``RESET ALL``).
    ``application_name`` doubles as the liveness beacon ``sweep_orphan_schemas``
    looks for in ``pg_stat_activity``. Extra keyword arguments go straight to
    ``create_async_engine``.
    """
    _check_identifier(schema)
    if poolclass is not None:
        engine_kwargs["poolclass"] = poolclass
    return create_async_engine(
        url,
        connect_args={
            "server_settings": {
                "search_path": f"{schema}, public",
                "application_name": schema,
            }
        },
        execution_options={"crypto_keys": _TEST_CRYPTO_KEYS},
        **engine_kwargs,
    )


def _wipe_sql(schema: str) -> str:
    """One DO block of child-first DELETEs over every ORM table, schema-qualified.

    Chosen by benchmark (300 iterations, tenant + account dirty, median / p95):
    DO-block DELETEs 7.8 / 12.4 ms; 44 separate DELETE round trips 31.8 / 40.8 ms;
    EXISTS-guarded DO block 9.9 / 16.1 ms; one TRUNCATE of all 44 tables
    139.8 / 162.4 ms (relfilenode churn). Child-first order (``reversed(
    sorted_tables)``) is required: two FKs are ON DELETE RESTRICT. Qualifying
    every name means the wipe can never resolve to ``public``.
    """
    deletes = " ".join(
        f'DELETE FROM "{schema}"."{table.name}";' for table in reversed(Base.metadata.sorted_tables)
    )
    return f"DO $$ BEGIN {deletes} END $$"


def _is_lock_timeout(err: DBAPIError) -> bool:
    # The asyncpg dialect stamps the SQLSTATE on the error it translates to.
    sqlstate: str | None = getattr(err.orig, "sqlstate", None)
    return sqlstate == _LOCK_NOT_AVAILABLE_SQLSTATE


async def truncate_all(conn: AsyncConnection, schema: str, *, lock_timeout: str = "5s") -> None:
    """Delete every row of every ORM table in ``schema`` within the current transaction.

    Raises ``RuntimeError`` (mentioning "timed out") when another connection
    still holds a lock on one of the tables after ``lock_timeout`` — the
    signature of a session or transaction an earlier test left open.
    """
    _check_identifier(schema)
    if not _INTERVAL_PATTERN.match(lock_timeout):
        raise ValueError(f"lock_timeout must look like '5s' or '200ms', got {lock_timeout!r}")
    await conn.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout}'"))
    try:
        await conn.execute(text(_wipe_sql(schema)))
    except DBAPIError as err:
        if _is_lock_timeout(err):
            raise RuntimeError(
                f"wiping test schema {schema!r} timed out after {lock_timeout}: another "
                "connection still holds a lock on one of its tables (a session or "
                "transaction left open by an earlier test?)"
            ) from err
        raise


async def sweep_orphan_schemas(engine: AsyncEngine, *, limit: int = 8) -> list[str]:
    """Drop up to ``limit`` per-worker schemas whose owning process is gone.

    A ``test_[wf]<pid>_<hex>`` schema is an orphan when no backend on this
    database reports it as ``application_name`` AND ``pid`` is not alive on
    this host. Legacy ``test_<32hex>`` names are never touched here
    (``scripts/db/sweep_test_schemas.py --include-legacy`` handles those).
    One sweeper runs at a time, serialised by a session-level advisory lock
    held on a dedicated connection; a caller that loses the lock does nothing.
    Each orphan is dropped in small transactions (see ``_drop_schema``).
    Returns the dropped names.
    """
    async with engine.connect() as lock_conn:
        took_lock = (
            await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": _SWEEP_LOCK_KEY}
            )
        ).scalar_one()
        if not took_lock:
            return []
        try:
            candidates: Sequence[str] = (
                (
                    await lock_conn.execute(
                        text(
                            "SELECT nspname FROM pg_namespace "
                            "WHERE nspname ~ :pattern "
                            "AND nspname NOT IN ("
                            "  SELECT application_name FROM pg_stat_activity "
                            "  WHERE datname = current_database()"
                            ") ORDER BY nspname"
                        ),
                        {"pattern": WORKER_SCHEMA_PATTERN.pattern},
                    )
                )
                .scalars()
                .all()
            )
            dropped: list[str] = []
            for schema in candidates:
                if len(dropped) >= limit:
                    break
                match = WORKER_SCHEMA_PATTERN.match(schema)
                if match is None or _pid_is_alive(int(match.group(1))):
                    continue
                await _drop_schema(engine, schema)
                dropped.append(schema)
            return dropped
        finally:
            await lock_conn.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": _SWEEP_LOCK_KEY}
            )
            await lock_conn.commit()


async def _require_migrated(conn: AsyncConnection) -> None:
    has_alembic = (
        await conn.execute(
            text("SELECT to_regclass('public.alembic_version') IS NOT NULL AS has_alembic")
        )
    ).scalar_one()
    if not has_alembic:
        raise RuntimeError(
            "alembic_version table is missing on the test DB. Run "
            "`uv run alembic upgrade head` from the repo root against "
            "DAIMON_DATABASE_URL=<test DSN> before invoking pytest."
        )


async def _create_schema_with_tables(engine: AsyncEngine, schema: str) -> None:
    """CREATE SCHEMA + create_all into it, then prove every ORM table landed.

    ``create_all(checkfirst=True)`` resolves unqualified names through
    ``pg_table_is_visible()``; with ``public`` on the search_path every table
    is "visible" already and no DDL would be emitted. The ``schema_translate_map``
    makes the existence checks and the DDL name the target schema explicitly.
    Tables go in dependency-ordered chunks so no transaction holds more than
    a fraction of the lock table (see module docstring).
    """
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    tables = Base.metadata.sorted_tables
    for start in range(0, len(tables), _CREATE_CHUNK_SIZE):
        async with engine.begin() as conn:
            mapped = await conn.execution_options(schema_translate_map={None: schema})
            await mapped.run_sync(
                Base.metadata.create_all, tables=tables[start : start + _CREATE_CHUNK_SIZE]
            )
    async with engine.connect() as conn:
        created = (
            await conn.execute(
                text("SELECT count(*) FROM pg_tables WHERE schemaname = :schema"),
                {"schema": schema},
            )
        ).scalar_one()
    expected = len(Base.metadata.tables)
    if created != expected:
        raise RuntimeError(
            f"create_all built {created} of {expected} ORM tables in schema {schema!r}"
        )


async def _drop_schema(engine: AsyncEngine, schema: str) -> None:
    """Drop ``schema`` a few tables per transaction, child-first, then the schema itself.

    Keeps every transaction's lock footprint small so many workers can tear
    down at once, with few enough commits that their WAL fsyncs don't pile
    up; the final ``DROP SCHEMA ... CASCADE`` only has non-ORM leftovers
    (sequences, tables a test created) to clean up.
    """
    _check_identifier(schema)
    tables = list(reversed(Base.metadata.sorted_tables))
    for start in range(0, len(tables), _DROP_CHUNK_SIZE):
        names = ", ".join(
            f'"{schema}"."{table.name}"' for table in tables[start : start + _DROP_CHUNK_SIZE]
        )
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {names} CASCADE"))
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.fixture(scope="session")
def db_schema() -> str:
    """This worker's schema name, ``test_w<pid>_<8hex>``."""
    return _new_schema_name("w")


@pytest_asyncio.fixture(scope="session")
async def db_engine(db_schema: str) -> AsyncIterator[AsyncEngine]:
    """Session-scoped pooled engine pinned to the worker schema.

    Assumes ``alembic upgrade head`` has already been applied to the test DB
    (CI runs it explicitly; local dev runs it once). Sweeps a few orphaned
    worker schemas, builds this worker's schema once, drops it at session end.
    """
    url = _require_test_dsn()
    engine = build_test_engine(url, db_schema, pool_size=2, max_overflow=0, pool_timeout=30)
    try:
        async with engine.connect() as conn:
            await _require_migrated(conn)
        await sweep_orphan_schemas(engine)
        await _create_schema_with_tables(engine, db_schema)
    except BaseException:
        await engine.dispose()
        raise
    try:
        yield engine
    finally:
        await _drop_schema(engine, db_schema)
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def db_nullpool_engine(db_engine: AsyncEngine, db_schema: str) -> AsyncIterator[AsyncEngine]:
    """NullPool twin of ``db_engine`` on the same schema.

    For code that opens its own event loop per call (Typer commands under
    ``CliRunner`` run ``asyncio.run``): asyncpg connections are loop-bound, so
    nothing may be pooled across calls. Depends on ``db_engine`` so the schema
    exists for its whole life.
    """
    engine = build_test_engine(_require_test_dsn(), db_schema, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_clean(
    db_engine: AsyncEngine, db_schema: str, request: pytest.FixtureRequest
) -> AsyncIterator[None]:
    """Wipe every ORM table in the worker schema before the test runs.

    Runs at setup, when every earlier fixture has torn down and all pool
    connections are checked in. A no-op under ``@pytest.mark.fresh_schema``.
    """
    global _last_db_test
    item = _request_item(request)
    if item.get_closest_marker("fresh_schema") is None:
        try:
            async with db_engine.begin() as conn:
                await truncate_all(conn, db_schema, lock_timeout="5s")
        except RuntimeError as err:
            raise RuntimeError(
                f"{err}; the previous DB test in this worker was {_last_db_test}"
            ) from err
    try:
        yield
    finally:
        from daimon.core.turn.outcomes import drain_outcomes

        await drain_outcomes()
        _last_db_test = item.nodeid


@asynccontextmanager
async def _fresh_schema_session(url: str) -> AsyncIterator[AsyncSession]:
    """A session on a private ``test_f<pid>_<8hex>`` schema, dropped afterwards."""
    schema = _new_schema_name("f")
    engine = build_test_engine(url, schema, poolclass=NullPool)
    try:
        await _create_schema_with_tables(engine, schema)
        try:
            async with engine.connect() as conn:
                session = AsyncSession(bind=conn, expire_on_commit=False)
                try:
                    yield session
                finally:
                    await session.close()
                    await conn.rollback()
        finally:
            await _drop_schema(engine, schema)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(
    db_engine: AsyncEngine,
    db_clean: None,
    request: pytest.FixtureRequest,
) -> AsyncIterator[AsyncSession]:
    """Per-test AsyncSession on one checked-out connection of the worker engine.

    The schema was wiped by ``db_clean`` just before. Under
    ``@pytest.mark.fresh_schema`` the session lives on a private throwaway
    schema instead (see module docstring).
    """
    if _request_item(request).get_closest_marker("fresh_schema") is not None:
        async with _fresh_schema_session(_require_test_dsn()) as session:
            yield session
        return
    async with db_engine.connect() as conn:
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await conn.rollback()


@pytest_asyncio.fixture
async def db_session_factory(
    db_session: AsyncSession,
) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the same connection as ``db_session``.

    The orchestrator opens fresh ``async with session_factory() as s, s.begin():``
    transactions per resource. Binding to ``db_session.bind`` (the underlying
    connection) means factory-created sessions share that connection, so their
    commits are visible to subsequent factory sessions within the same test.
    """
    return async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
