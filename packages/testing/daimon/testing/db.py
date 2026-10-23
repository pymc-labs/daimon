"""DB fixtures for daimon test suites.

Import these into a package conftest to make them discoverable by pytest:

    # tests/conftest.py
    from daimon.testing.db import (  # noqa: F401
        db_clean,
        db_committing_engine,
        db_committing_pool,
        db_engine,
        db_nullpool_engine,
        db_schema,
        db_session,
        db_session_factory,
    )

Model:

- One external Postgres (the compose service) and one dedicated test database
  on local PostgreSQL named ``daimon_test_nc_*``, or ``daimon_test`` in exact
  GitHub Actions CI with a registered process-owned schema. A shared pytest
  preflight refuses other targets before engine fixtures run. Migrations must
  already be applied there. Plain local ``daimon_test`` fixtures skip.
- Each resolved worker schema, ``test_w<pid>_<8hex>``, is built once per pytest
  session (``CREATE SCHEMA`` + ``Base.metadata.create_all``). Fixture aliases
  share its engine; the last fixture user drops it. The pid + nonce keep two
  worktrees' ``gw0`` workers apart on the shared database.
- Every connection the worker engine opens carries ``search_path = <schema>,
  public`` and ``application_name = <schema>`` as asyncpg *server settings*
  (startup-packet defaults, so they survive pool reuse and ``RESET``). Nothing
  ever issues ``SET search_path`` on a ``db_engine`` connection.
- Per test, ``db_clean`` wipes every ORM table in the worker schema at SETUP
  (one DO block of child-first DELETEs, a few ms). ``public`` is never touched:
  other worktrees share it, and a few own-engine tests write there on purpose.
- ``@pytest.mark.fresh_schema`` opts a test out of the shared schema, for tests
  that run DDL. It gets one reusable per-worker DDL schema, ``test_f<pid>_<8hex>``,
  built on first use. The test runs in a transaction ``db_session`` rolls back, and
  Postgres rolls DDL back with it. The next such test first compares a catalog
  fingerprint with the baseline: unchanged, it deletes every row; changed (a test
  committed DDL), it drops and rebuilds the schema. ``DAIMON_TEST_FRESH_SCHEMA=1``
  restores a private schema per test (CREATE / create_all / DROP around it).
- ``db_committing_engine`` is a pooled engine (production's 5 + 10) on the worker
  schema for tests whose sessions really COMMIT; ``db_nullpool_engine`` (a new
  connection per checkout, ~20 ms each) remains for code that runs its own event
  loop per call and for session-level advisory locks.
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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal, cast
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from daimon.core import platform_names
from daimon.core._models import Base
from sqlalchemy import event, text
from sqlalchemy.dialects.postgresql import asyncpg as asyncpg_dialect
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry, NullPool, Pool, QueuePool

#: Names this module creates: ``test_w<pid>_<8hex>`` (worker) / ``test_f<pid>_<8hex>`` (fresh).
WORKER_SCHEMA_PATTERN = re.compile(r"^test_[wf](\d+)_[0-9a-f]{8}$")
_IDENTIFIER_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")
_INTERVAL_PATTERN = re.compile(r"^\d+(ms|s|min)?$")
_SWEEP_LOCK_KEY = "daimon:test-schema-sweep"
_LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
_DEADLOCK_DETECTED_SQLSTATE = "40P01"
# A wipe that loses a deadlock to an earlier test's late writer retries this often.
_WIPE_ATTEMPTS = 3
_CREATE_CHUNK_SIZE = 8
_DROP_CHUNK_SIZE = 4
_LOCK_TIMEOUT = "60s"

# nodeid of the last test that used the DB in this worker; named in the error
# when the next test's wipe times out on a lock that test left behind.
_last_db_test: str | None = None
_TEST_CRYPTO_KEYS = (Fernet.generate_key().decode("ascii"),)
_TEST_ENGINE_SCHEMAS: WeakKeyDictionary[AsyncEngine, str] = WeakKeyDictionary()
_OWNED_DATABASE_REFUSAL = (
    "Test hooks require owned local lane engines or registered isolated CI schemas"
)


_DialectConnection = asyncpg_dialect.AsyncAdapt_asyncpg_connection


def _end_transactions_under_the_statement_mutex() -> None:
    """Make COMMIT and ROLLBACK on a shared test connection wait for its running statement.

    Several fixtures hand one ``AsyncConnection`` to more than one task (the test
    body, a bot's background sweep, a teardown ``rollback``). SQLAlchemy's asyncpg
    dialect serialises statements with ``_execute_mutex`` but not the commit and
    rollback that end the transaction. Two tasks ending it together, with a third
    starting the next one, interleave like this::

        T2: ROLLBACK sent        T1: ROLLBACK -> InterfaceError (operation in progress)
        T0: BEGIN sent and acknowledged; asyncpg now holds T0's transaction
        T2: ROLLBACK returns; the dialect clears its ``_transaction`` / ``_started``

    SQLAlchemy has forgotten a transaction asyncpg still holds. The connection
    goes back to the pool mid-transaction, the pool's rollback is skipped, and
    the next user's ``COMMIT`` is a ``RELEASE SAVEPOINT`` inside it. Its row and
    advisory locks stay held, so the sibling pooled connection's next wipe waits
    on them until the job times out. Production gives each task its own
    connection, so only tests see this. Taking the statement mutex around both
    calls removes the interleaving; ``_guard_pooled_transactions`` still reports
    a connection that comes back mid-transaction for any other reason.
    """
    connection_class = _DialectConnection
    if getattr(connection_class, "_daimon_serialised_transaction_end", False):
        return
    for name in ("_rollback_and_discard", "_commit_and_discard"):
        end_transaction = getattr(connection_class, name, None)
        if end_transaction is None:
            raise RuntimeError(
                f"SQLAlchemy's asyncpg dialect no longer has {name}; revisit "
                "_end_transactions_under_the_statement_mutex in daimon.testing.db"
            )
        setattr(connection_class, name, _under_statement_mutex(end_transaction))
    connection_class._daimon_serialised_transaction_end = True  # pyright: ignore[reportAttributeAccessIssue]


def _under_statement_mutex(
    end_transaction: Callable[[_DialectConnection], Awaitable[None]],
) -> Callable[[_DialectConnection], Awaitable[None]]:
    async def serialised(adapted_connection: _DialectConnection) -> None:
        async with adapted_connection._execute_mutex:  # pyright: ignore[reportPrivateUsage]
            await end_transaction(adapted_connection)

    serialised.__name__ = end_transaction.__name__
    return serialised


_end_transactions_under_the_statement_mutex()


@dataclass(frozen=True)
class DatabasePreflight:
    """Validated fixture configuration; never expose credentials in its repr."""

    url: str = field(repr=False)
    schema: str


@dataclass
class _DdlSchema:
    """A worker's reusable ``@pytest.mark.fresh_schema`` schema (see ``db_session``)."""

    name: str
    engine: AsyncEngine = field(repr=False)
    baseline: str = field(repr=False)


@dataclass
class _WorkerEngine:
    engine: AsyncEngine = field(repr=False)
    users: int = 0
    ddl: _DdlSchema | None = None


_WORKER_ENGINES = pytest.StashKey[dict[tuple[URL, str], _WorkerEngine]]()


def _require_owned_database(url: URL, schema: str | None) -> None:
    """Common URL/process-schema policy, with no engine allocation or I/O.

    Engine admission supplies ONLY builder-registered schema provenance here.
    Preflight supplies the future fixture schema; the actual engine must still
    pass require_owned_sessionmaker before its first connection.
    """
    schema_match = WORKER_SCHEMA_PATTERN.fullmatch(schema) if schema is not None else None
    isolated_ci = (
        url.database == "daimon_test"
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("CI") == "true"
        and schema_match is not None
        and int(schema_match[1]) == os.getpid()
    )
    if (
        url.get_backend_name() != "postgresql"
        or url.host not in ("localhost", "127.0.0.1")
        or url.query
        or not (
            re.fullmatch(r"daimon_test_nc_[A-Za-z0-9][A-Za-z0-9_]{0,47}", url.database or "")
            or isolated_ci
        )
    ):
        raise ValueError(_OWNED_DATABASE_REFUSAL)


def preflight_test_database(schema: str) -> DatabasePreflight:
    """Validate the fixture DSN before allocating any engine or connection.

    The contributor's ordinary local daimon_test skips outside exact CI. All
    other unowned targets fail closed. Actual engines retain the #756 builder
    provenance check; preflight is not a grant for an injected/routed engine.
    """
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        raise RuntimeError("DAIMON_DATABASE__TEST_URL must be set to run daimon tests.")
    try:
        parsed = make_url(url)
    except (ArgumentError, ValueError):
        raise ValueError(_OWNED_DATABASE_REFUSAL) from None
    _check_identifier(schema)
    if (
        parsed.get_backend_name() == "postgresql"
        and parsed.host in ("localhost", "127.0.0.1")
        and not parsed.query
        and parsed.database == "daimon_test"
        and not (os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true")
    ):
        pytest.skip(
            "Shared DB fixtures require an owned lane database or isolated GitHub Actions CI"
        )
    _require_owned_database(parsed, schema)
    return DatabasePreflight(url, schema)


def _require_test_dsn() -> str:  # pyright: ignore[reportUnusedFunction]  # retained for existing test harness imports
    """Read the test DSN and refuse to proceed unless it contains 'test'."""
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        raise RuntimeError("DAIMON_DATABASE__TEST_URL must be set to run daimon tests.")
    db_name = urlparse(url).path.lstrip("/")
    if "test" not in db_name:
        raise RuntimeError(
            "Refusing destructive fixtures: database name must contain the substring 'test'."
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
    engine = create_async_engine(
        url,
        connect_args={
            "server_settings": {
                "search_path": f"{schema}, public",
                "application_name": schema,
                # A test whose own connections wait on each other (an orphaned
                # background write against the test's open transaction) fails
                # in a minute with the lock named, instead of idling to the job
                # timeout. Tests that wait for a lock on purpose set a shorter one.
                "lock_timeout": _LOCK_TIMEOUT,
            }
        },
        execution_options={"crypto_keys": _TEST_CRYPTO_KEYS},
        **engine_kwargs,
    )
    _TEST_ENGINE_SCHEMAS[engine] = schema
    return engine


def sibling_nullpool_engine(engine: AsyncEngine) -> AsyncEngine:
    """A new NullPool engine on the same database and schema as ``engine``.

    For code under test that disposes the engine it is handed: disposing a shared
    pooled engine replaces its pool, which the next test would inherit. The caller
    owns the sibling and may let that code dispose it.
    """
    schema = isolated_test_schema(engine)
    if schema is None:
        raise ValueError("sibling_nullpool_engine needs an engine from build_test_engine")
    return build_test_engine(
        engine.url.render_as_string(hide_password=False), schema, poolclass=NullPool
    )


def isolated_test_schema(engine: AsyncEngine) -> str | None:
    """Return schema provenance only for an engine made by build_test_engine.

    An execution-option string or a lookalike URL is not builder provenance.
    This lookup never connects; callers must also check the owned schema pattern.
    """
    return _TEST_ENGINE_SCHEMAS.get(engine)


def require_owned_sessionmaker(sm_or_engine: object) -> None:
    """Refuse foreign main/routed binds without acquiring any connection.

    Local PostgreSQL lane databases are admitted. The sole shared-name exception
    is GitHub Actions CI with a builder-registered schema owned by this process.
    Connection binds inherit their actual engine's policy; unbound factories,
    synchronous binds and malformed routing maps are refused. No defaults change.
    """
    refusal = _OWNED_DATABASE_REFUSAL
    bindings: tuple[object, ...]
    if isinstance(sm_or_engine, AsyncEngine):
        bindings = (sm_or_engine,)
    elif isinstance(sm_or_engine, async_sessionmaker):
        factory = cast(async_sessionmaker[AsyncSession], sm_or_engine)
        kwargs = cast(dict[str, object], factory.kw)
        routed = kwargs.get("binds")
        if routed is None:
            bindings = (kwargs.get("bind"),)
        elif isinstance(routed, Mapping):
            bindings = (kwargs.get("bind"), *cast(Mapping[object, object], routed).values())
        else:
            raise ValueError(refusal)
    else:
        raise ValueError(refusal)

    for binding in bindings:
        if isinstance(binding, AsyncConnection):
            engine = binding.engine
        elif isinstance(binding, AsyncEngine):
            engine = binding
        else:
            raise ValueError(refusal)
        _require_owned_database(engine.url, isolated_test_schema(engine))


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


def _is_deadlock(err: DBAPIError) -> bool:
    sqlstate: str | None = getattr(err.orig, "sqlstate", None)
    return sqlstate == _DEADLOCK_DETECTED_SQLSTATE


async def wipe_schema(engine: AsyncEngine, schema: str, *, lock_timeout: str = "5s") -> None:
    """``truncate_all`` in its own transaction, retried when Postgres picks it as a deadlock victim.

    The child-first DELETEs can deadlock with a write an earlier test left running
    (its FK check holds ``FOR KEY SHARE`` on a parent row the wipe deletes later,
    while it waits on a row the wipe already deleted). Postgres aborts one side; when
    it is the wipe, the other side commits and a retry deletes its rows too.
    """
    for attempt in range(1, _WIPE_ATTEMPTS + 1):
        try:
            async with engine.begin() as conn:
                await truncate_all(conn, schema, lock_timeout=lock_timeout)
            return
        except DBAPIError as err:
            if not _is_deadlock(err) or attempt == _WIPE_ATTEMPTS:
                raise


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
async def db_engine(
    db_preflight: DatabasePreflight, db_schema: str, request: pytest.FixtureRequest
) -> AsyncIterator[AsyncEngine]:
    """Session-scoped pooled engine pinned to the worker schema.

    Assumes ``alembic upgrade head`` has already been applied to the test DB
    (CI runs it explicitly; local dev runs it once). Sweeps a few orphaned
    worker schemas, builds each worker schema once, and drops it on final release.
    Local aliases/re-exports are distinct pytest fixture definitions but share
    the same engine when their admitted database and schema match.
    """
    engines = request.config.stash.setdefault(_WORKER_ENGINES, {})
    key = (make_url(db_preflight.url), db_schema)
    worker = engines.get(key)
    if worker is None:
        engine = build_test_engine(
            db_preflight.url, db_schema, pool_size=2, max_overflow=0, pool_timeout=30
        )
        try:
            require_owned_sessionmaker(engine)
            _guard_pooled_transactions(engine)
            async with engine.connect() as conn:
                await _require_migrated(conn)
            await sweep_orphan_schemas(engine)
            await _create_schema_with_tables(engine, db_schema)
        except BaseException:
            await engine.dispose()
            raise
        worker = _WorkerEngine(engine)
        engines[key] = worker
    else:
        require_owned_sessionmaker(worker.engine)
    worker.users += 1
    try:
        yield worker.engine
    finally:
        worker.users -= 1
        if worker.users == 0:
            del engines[key]
            try:
                await _drain_background_tasks()
                if worker.ddl is not None:
                    try:
                        await _drop_schema(worker.ddl.engine, worker.ddl.name)
                    finally:
                        await worker.ddl.engine.dispose()
                await _drop_schema(worker.engine, db_schema)
            finally:
                await worker.engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def db_nullpool_engine(
    db_preflight: DatabasePreflight, db_engine: AsyncEngine, db_schema: str
) -> AsyncIterator[AsyncEngine]:
    """NullPool twin of ``db_engine`` on the same schema.

    For code that opens its own event loop per call (Typer commands under
    ``CliRunner`` run ``asyncio.run``): asyncpg connections are loop-bound, so
    nothing may be pooled across calls. Depends on ``db_engine`` so the schema
    exists for its whole life.
    """
    engine = build_test_engine(db_preflight.url, db_schema, poolclass=NullPool)
    try:
        require_owned_sessionmaker(engine)
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def db_committing_pool(
    db_preflight: DatabasePreflight, db_engine: AsyncEngine, db_schema: str
) -> AsyncIterator[AsyncEngine]:
    """Session-scoped pooled engine for committing sessions (see ``db_committing_engine``).

    Sized like production's ``build_engine`` default (5 + 10), so code that gates
    on its pool (``session_preparation_gate.pool_headroom``) takes the same path
    as in production, and four xdist workers stay under the stock
    ``max_connections = 100``. ``pool_pre_ping`` replaces a connection a test
    killed with ``pg_terminate_backend``, and every connection returns to the pool
    with its transaction rolled back (``pool_reset_on_return="rollback"``, spelled
    out because the isolation contract rests on it).
    ``DAIMON_TEST_NULLPOOL=1`` restores one fresh connection per checkout.
    """
    if os.environ.get("DAIMON_TEST_NULLPOOL") == "1":
        engine = build_test_engine(db_preflight.url, db_schema, poolclass=NullPool)
    else:
        engine = build_test_engine(
            db_preflight.url,
            db_schema,
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_pre_ping=True,
            pool_reset_on_return="rollback",
        )
        _watch_pool(engine)
        _guard_pooled_transactions(engine)
    try:
        require_owned_sessionmaker(engine)
        await _record_session_baseline(engine, db_schema)
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_committing_engine(
    db_committing_pool: AsyncEngine, db_schema: str, request: pytest.FixtureRequest
) -> AsyncIterator[AsyncEngine]:
    """Pooled engine on the worker schema for tests whose sessions really COMMIT.

    Use it where code under test opens its own sessions (``sm.begin()``) or a
    test needs several independent connections, and everything runs on the
    session event loop. Each checkout is a fresh transaction on a warm
    connection, so statements stay prepared across tests; opening a new
    connection per session (``db_nullpool_engine``) costs about 20 ms each.

    Keep ``db_nullpool_engine`` for what pooling would change:

    - code that runs ``asyncio.run`` per call (Typer commands under
      ``CliRunner``): asyncpg connections are bound to the loop that opened them;
    - session-level advisory locks, which Postgres releases only when the
      connection closes, or tests that kill a backend with
      ``pg_terminate_backend``.

    At teardown the pool must be clean: every connection checked in, no session
    fence permit held, and every connection the test touched back in its
    starting session state (``search_path``, no ``SET`` GUC, temp object,
    ``LISTEN`` or session-level advisory lock). A test that leaks one fails here,
    and the connection is repaired, not left to fail an unrelated later test.
    """
    pool = db_committing_pool.pool
    await _drain_background_tasks()
    baseline = _pool_checkedout(pool)
    _forget_touched_connections(pool)
    yield db_committing_pool
    await _drain_background_tasks()
    _raise_on_leaked_transactions(pool, _request_item(request).nodeid)
    await _assert_committing_pool_clean(db_committing_pool, pool, baseline, db_schema)


async def _assert_committing_pool_clean(
    engine: AsyncEngine, pool: Pool, baseline: int, schema: str
) -> None:
    """Raise when a test left ``engine`` (whose pool was ``pool``) dirty for the next one."""
    problems: list[str] = []
    if engine.pool is not pool:
        problems.append("the test replaced the engine's pool")
    if (leaked := _pool_checkedout(pool) - baseline) > 0:
        problems.append(f"{leaked} pooled connection(s) are still checked out")
    if (stuck := _pool_gate_deficit(pool)) is not None:
        problems.append(f"a session fence permit was never released ({stuck})")
    if problems:
        raise AssertionError(
            f"test left db_committing_engine dirty: {'; '.join(problems)}; close its sessions "
            "and release its fences, or use db_nullpool_engine"
        )
    if leaks := await _repair_touched_connections(pool):
        raise AssertionError(
            f"test left session state on pooled connection(s): {'; '.join(leaks)}. A pooled "
            "connection keeps it for the next test; undo it (RESET, DROP, UNLISTEN, advisory "
            "unlock), use SET LOCAL, or use db_nullpool_engine"
        )


# The session settings a test may SET and forget. Their values are compared with a
# brand-new connection's, so a startup-packet setting (``search_path``,
# ``lock_timeout``) counts as changed when it differs from what the pool opens
# connections with. ``current_setting`` is free; ``pg_settings`` would cost 0.6 ms.
_WATCHED_GUCS = (
    "search_path",
    "statement_timeout",
    "lock_timeout",
    "idle_in_transaction_session_timeout",
    "TimeZone",
    "DateStyle",
    "role",
    "default_transaction_isolation",
    "default_transaction_read_only",
    "synchronous_commit",
    "work_mem",
    "client_min_messages",
    "application_name",
)

# One round trip per connection a test touched. A temp table makes
# ``pg_my_temp_schema()`` non-empty; on a connection outside any transaction every
# advisory lock is session-level.
_SESSION_STATE_SQL = (
    "SELECT "
    + ", ".join(f"current_setting('{name}') AS \"{name}\"" for name in _WATCHED_GUCS)
    + """,
  (SELECT count(*) FROM pg_class WHERE relnamespace = pg_my_temp_schema()) AS temp_objects,
  (SELECT count(*) FROM pg_listening_channels()) AS channels,
  (SELECT count(*) FROM pg_locks
    WHERE locktype = 'advisory' AND granted AND pid = pg_backend_pid()) AS advisory_locks
"""
)

# Pooled connections each test touched, kept by the pool's checkout event.
_POOL_WATCHES: WeakKeyDictionary[Pool, dict[int, ConnectionPoolEntry]] = WeakKeyDictionary()
# What a brand-new connection of the pool reports for ``_WATCHED_GUCS``.
_POOL_BASELINES: WeakKeyDictionary[Pool, dict[str, str]] = WeakKeyDictionary()


def _watch_pool(engine: AsyncEngine) -> None:
    """Record every connection checked out of ``engine``'s pool; refuse one mid-transaction.

    NullPool (the kill switch) closes each connection on checkin, so it carries
    nothing to the next test and needs no watch.
    """
    pool = engine.pool
    if not isinstance(pool, QueuePool):
        return
    touched = _POOL_WATCHES.setdefault(pool, {})

    @event.listens_for(engine.sync_engine, "checkout")
    def _on_checkout(  # pyright: ignore[reportUnusedFunction]
        dbapi_connection: object, record: ConnectionPoolEntry, proxy: object
    ) -> None:
        raw = record.driver_connection
        if raw is not None and raw.is_in_transaction():  # pyright: ignore[reportUnknownMemberType]
            raise AssertionError(
                "a pooled connection came back from the pool inside a transaction: "
                "its previous user did not roll back"
            )
        touched[id(record)] = record


# Pooled connections a pool took back while their server-side transaction was
# still open, with the state that gave it away. Filled by ``_guard_pooled_transactions``,
# emptied by ``_raise_on_leaked_transactions``.
_POOL_TRANSACTION_LEAKS: WeakKeyDictionary[Pool, list[str]] = WeakKeyDictionary()


def _guard_pooled_transactions(engine: AsyncEngine) -> None:
    """Close any connection ``engine``'s pool takes back with a transaction still open.

    ``pool_reset_on_return="rollback"`` rolls back only a transaction SQLAlchemy
    still tracks. When the asyncpg connection is mid-transaction without SQLAlchemy
    knowing (a ``BEGIN`` it lost track of, a nested asyncpg transaction left behind
    by an earlier one), the rollback is skipped and the next user's every ``COMMIT``
    becomes a ``RELEASE SAVEPOINT`` inside the stale transaction. Its row and
    advisory locks then stay held, so the sibling pool connection's next wipe waits
    on them until the job times out. The connection is terminated here, which frees
    those locks and makes the pool open a fresh one; the test that was running is
    named by ``_raise_on_leaked_transactions`` at its teardown.
    """
    pool = engine.pool
    if not isinstance(pool, QueuePool):
        return
    leaks = _POOL_TRANSACTION_LEAKS.setdefault(pool, [])

    @event.listens_for(engine.sync_engine, "checkin")
    def _on_checkin(  # pyright: ignore[reportUnusedFunction]
        dbapi_connection: object, record: ConnectionPoolEntry
    ) -> None:
        raw = record.driver_connection
        if raw is None or raw.is_closed():  # pyright: ignore[reportUnknownMemberType]
            return
        in_transaction = raw.is_in_transaction()  # pyright: ignore[reportUnknownMemberType]
        outer = raw._top_xact  # pyright: ignore[reportUnknownMemberType, reportPrivateUsage]
        if in_transaction or outer is not None:
            leaks.append(
                f"backend pid {raw.get_server_pid()} was returned to the pool "  # pyright: ignore[reportUnknownMemberType]
                f"with the server transaction {'open' if in_transaction else 'closed'} and "
                f"asyncpg's transaction {'still registered' if outer is not None else 'cleared'}"
            )
            record.invalidate()


def _raise_on_leaked_transactions(pool: Pool, nodeid: str) -> None:
    """Fail the test named ``nodeid`` when it returned a connection mid-transaction."""
    if (leaks := _POOL_TRANSACTION_LEAKS.get(pool)) and (found := list(leaks)):
        leaks.clear()
        raise AssertionError(
            f"{nodeid} returned a pooled connection to the worker pool inside an open "
            f"transaction: {'; '.join(found)}. Its locks would have stalled every later "
            "test on this worker, so the connection was terminated. Find the session, "
            "connection or task of this test that began a transaction and never ended it."
        )


async def _record_session_baseline(engine: AsyncEngine, schema: str) -> None:
    """Remember the settings a brand-new connection of ``engine``'s pool starts with."""
    pool = engine.pool
    if not isinstance(pool, QueuePool):
        return
    async with engine.connect() as conn:
        fairy = await conn.get_raw_connection()
        raw = fairy.driver_connection
        row = await raw.fetchrow(_SESSION_STATE_SQL)  # pyright: ignore[reportUnknownMemberType, reportOptionalMemberAccess]
    baseline = {name: str(row[name]) for name in _WATCHED_GUCS}
    if baseline["search_path"] != f"{schema}, public":
        raise AssertionError(
            f"pooled connections open with search_path {baseline['search_path']!r}"
        )
    _POOL_BASELINES[pool] = baseline
    _forget_touched_connections(pool)


def _forget_touched_connections(pool: Pool) -> None:
    if (touched := _POOL_WATCHES.get(pool)) is not None:
        touched.clear()


async def _repair_touched_connections(pool: Pool) -> list[str]:
    """Check every connection the test touched against a fresh session; reset the ones that differ.

    Returns one description per dirty connection. A connection still checked out
    is skipped: the checked-out count already reports it.
    """
    touched = _POOL_WATCHES.get(pool)
    if not touched:
        return []
    records = list(touched.values())
    touched.clear()
    leaks: list[str] = []
    for record in records:
        raw = record.driver_connection  # pyright: ignore[reportUnknownMemberType]
        if record.in_use or raw is None or raw.is_closed():  # pyright: ignore[reportUnknownMemberType]
            continue
        row = await raw.fetchrow(_SESSION_STATE_SQL)  # pyright: ignore[reportUnknownMemberType]
        found: list[str] = []
        baseline = _POOL_BASELINES.get(pool, {})
        for name in _WATCHED_GUCS:
            if name in baseline and row[name] != baseline[name]:
                found.append(f"{name} is {row[name]!r}, not {baseline[name]!r}")
        if row["temp_objects"]:
            found.append(f"{row['temp_objects']} temp object(s)")
        if row["channels"]:
            found.append("LISTEN channel(s)")
        if row["advisory_locks"]:
            found.append(f"{row['advisory_locks']} session-level advisory lock(s)")
        if found:
            leaks.append(", ".join(found))
            await raw.execute("RESET ALL")  # pyright: ignore[reportUnknownMemberType]
            await raw.execute("DISCARD TEMP")  # pyright: ignore[reportUnknownMemberType]
            await raw.execute("UNLISTEN *")  # pyright: ignore[reportUnknownMemberType]
            await raw.execute("SELECT pg_advisory_unlock_all()")  # pyright: ignore[reportUnknownMemberType]
    return leaks


def _pool_checkedout(pool: Pool) -> int:
    # NullPool (the kill switch) keeps no count.
    return pool.checkedout() if isinstance(pool, QueuePool) else 0


def _pool_gate_deficit(pool: Pool) -> str | None:
    """Describe a session fence (``pool_headroom``) permit that is still held, else None.

    The gates are keyed by pool and live as long as it does, which here is the
    whole worker session: one test that never releases a permit would starve
    every later test through it.
    """
    from daimon.core import session_preparation_gate as fences

    mutation = fences._pool_gates.get(pool)  # pyright: ignore[reportPrivateUsage]
    preparation = fences._preparation_pool_gates.get(pool)  # pyright: ignore[reportPrivateUsage]
    if mutation is None or preparation is None or not isinstance(pool, QueuePool):
        return None
    holders = (pool.size() + max(0, pool._max_overflow)) // 2  # pyright: ignore[reportPrivateUsage]
    if holders == 1:
        return None if mutation._value == 1 else "single shared gate"  # pyright: ignore[reportPrivateUsage]
    prep = min(max(1, pool.size() // 2), holders - 1)
    want = {"preparation": prep, "mutation": holders - prep}
    have = {"preparation": preparation._value, "mutation": mutation._value}  # pyright: ignore[reportPrivateUsage]
    return None if have == want else f"{have} of {want}"


# Name writes (`daimon.core.platform_names`) run in the background in
# production. A test's sessions share one connection, which such a write would
# use concurrently with the code under test, so tests hold them for
# `_drain_background_tasks` (or an explicit `settle()`) to run one at a time.
platform_names.defer_writes()


@pytest_asyncio.fixture
async def db_clean(
    db_preflight: DatabasePreflight,
    db_engine: AsyncEngine,
    db_schema: str,
    request: pytest.FixtureRequest,
) -> AsyncIterator[None]:
    """Wipe every ORM table in the worker schema before the test runs.

    Runs at setup, when every earlier fixture has torn down and all pool
    connections are checked in. A no-op under ``@pytest.mark.fresh_schema``.
    """
    global _last_db_test
    require_owned_sessionmaker(db_engine)
    item = _request_item(request)
    # A test before this one may have left name writes running, or remembered
    # names the wipe below deletes.
    await _drain_background_tasks()
    _raise_on_leaked_transactions(db_engine.pool, f"{_last_db_test} (or a test between)")
    if item.get_closest_marker("fresh_schema") is None:
        try:
            await wipe_schema(db_engine, db_schema, lock_timeout="5s")
        except RuntimeError as err:
            raise RuntimeError(
                f"{err}; the previous DB test in this worker was {_last_db_test}"
            ) from err
    try:
        yield
    finally:
        await _drain_background_tasks()
        _last_db_test = item.nodeid
        _raise_on_leaked_transactions(db_engine.pool, item.nodeid)


async def _drain_background_tasks() -> None:
    """Finish a test's background database work before closing or wiping it.

    Face rendering uses a separate engine connection. Cancellation must finish
    its transaction rollback before any schema DDL. Other tasks write through
    the test's sessionmaker and must finish before its connection is returned.
    Forget cached names because the next wipe deletes them.
    """
    from daimon.core.agent_identity import cancel_and_wait_pending_agent_faces
    from daimon.core.channel_budget_notice import drain_budget_notices
    from daimon.core.turn.outcomes import drain_outcomes

    await cancel_and_wait_pending_agent_faces()
    await drain_outcomes()
    await drain_budget_notices()
    await platform_names.settle()
    platform_names.forget_all()


# Everything DDL can change in one schema: columns (type, nullability, default,
# identity, generation, collation), indexes, constraints, triggers, functions,
# views, sequences, enum and domain types. A rolled-back transaction leaves it
# untouched; a committed ALTER/CREATE/DROP does not. Row data is not part of it:
# every fresh test starts by deleting every row.
_FINGERPRINT_SQL = text(
    """
SELECT md5(concat_ws('|',
  (SELECT coalesce(string_agg(c.relname || '.' || a.attname || ':'
          || format_type(a.atttypid, a.atttypmod) || ':' || a.attnotnull::text || ':'
          || a.attidentity::text || a.attgenerated::text || ':'
          || coalesce((SELECT collname FROM pg_collation WHERE oid = a.attcollation), '') || ':'
          || coalesce(pg_get_expr(d.adbin, d.adrelid), ''),
          ',' ORDER BY c.relname, a.attnum), '')
     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
     LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
    WHERE n.nspname = :schema AND c.relkind IN ('r', 'p', 'v', 'm', 'S')),
  (SELECT coalesce(string_agg(pg_get_indexdef(i.indexrelid), ','
          ORDER BY i.indexrelid::regclass::text), '')
     FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid
     JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = :schema),
  (SELECT coalesce(string_agg(k.conname || ':' || pg_get_constraintdef(k.oid),
          ',' ORDER BY k.conname, k.oid), '')
     FROM pg_constraint k JOIN pg_namespace n ON n.oid = k.connamespace
    WHERE n.nspname = :schema),
  (SELECT coalesce(string_agg(pg_get_triggerdef(g.oid), ',' ORDER BY g.tgname, g.oid), '')
     FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid
     JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = :schema AND NOT g.tgisinternal),
  (SELECT coalesce(string_agg(pg_get_functiondef(p.oid), ',' ORDER BY p.proname, p.oid), '')
     FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = :schema AND p.prokind IN ('f', 'p')),
  (SELECT coalesce(string_agg(c.relname || ':' || pg_get_viewdef(c.oid), ',' ORDER BY c.relname),
          '')
     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = :schema AND c.relkind IN ('v', 'm')),
  (SELECT coalesce(string_agg(t.typname || ':' || t.typtype::text, ',' ORDER BY t.typname), '')
     FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = :schema AND t.typtype IN ('e', 'd')),
  (SELECT coalesce(string_agg(e.enumtypid::text || e.enumlabel, ',' ORDER BY e.oid), '')
     FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
     JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = :schema)
)) AS fingerprint
"""
)


async def _schema_fingerprint(engine: AsyncEngine, schema: str) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(_FINGERPRINT_SQL, {"schema": schema})).scalar_one()


async def _ready_ddl_schema(worker: _WorkerEngine, url: str) -> _DdlSchema:
    """The worker's DDL schema, empty and shaped exactly like the ORM models.

    Built on first use and reused: a ``fresh_schema`` test runs inside a
    transaction that ``db_session`` rolls back, and Postgres rolls DDL back with
    it. A test that commits anyway (``session.commit()``, ``engine.begin()``)
    leaves rows and maybe a changed shape; the next test rewipes the rows,
    restarts the sequences a test advanced, and rebuilds the schema when the
    shape differs from the baseline.
    """
    ddl = worker.ddl
    if ddl is not None and await _schema_fingerprint(ddl.engine, ddl.name) == ddl.baseline:
        async with ddl.engine.begin() as conn:
            await truncate_all(conn, ddl.name, lock_timeout="5s")
            # A rolled-back nextval() still advances its sequence; a private
            # schema would have started every test at 1.
            advanced = (
                await conn.execute(
                    text(
                        "SELECT sequencename FROM pg_sequences "
                        "WHERE schemaname = :schema AND last_value IS NOT NULL"
                    ),
                    {"schema": ddl.name},
                )
            ).scalars()
            for sequence in list(advanced):
                quoted = sequence.replace('"', '""')
                await conn.execute(text(f'ALTER SEQUENCE "{ddl.name}"."{quoted}" RESTART'))
        return ddl
    if ddl is not None:
        await _drop_schema(ddl.engine, ddl.name)
        await _create_schema_with_tables(ddl.engine, ddl.name)
        return ddl
    name = _new_schema_name("f")
    engine = build_test_engine(url, name, poolclass=NullPool)
    try:
        require_owned_sessionmaker(engine)
        await _create_schema_with_tables(engine, name)
        worker.ddl = _DdlSchema(name, engine, await _schema_fingerprint(engine, name))
    except BaseException:
        await engine.dispose()
        raise
    return worker.ddl


@asynccontextmanager
async def _ddl_schema_session(worker: _WorkerEngine, url: str) -> AsyncIterator[AsyncSession]:
    """A session on the worker's reusable DDL schema; its transaction is rolled back."""
    ddl = await _ready_ddl_schema(worker, url)
    async with ddl.engine.connect() as conn:
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await _drain_background_tasks()
            await session.close()
            await conn.rollback()


@asynccontextmanager
async def _fresh_schema_session(url: str) -> AsyncIterator[AsyncSession]:
    """A session on a private ``test_f<pid>_<8hex>`` schema, dropped afterwards."""
    schema = _new_schema_name("f")
    engine = build_test_engine(url, schema, poolclass=NullPool)
    try:
        require_owned_sessionmaker(engine)
        await _create_schema_with_tables(engine, schema)
        try:
            async with engine.connect() as conn:
                session = AsyncSession(bind=conn, expire_on_commit=False)
                try:
                    yield session
                finally:
                    await _drain_background_tasks()
                    await session.close()
                    await conn.rollback()
        finally:
            await _drop_schema(engine, schema)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(
    db_preflight: DatabasePreflight,
    db_engine: AsyncEngine,
    db_schema: str,
    db_clean: None,
    request: pytest.FixtureRequest,
) -> AsyncIterator[AsyncSession]:
    """Per-test AsyncSession on one checked-out connection of the worker engine.

    The schema was wiped by ``db_clean`` just before. Under
    ``@pytest.mark.fresh_schema`` the session lives on a private throwaway
    schema instead (see module docstring).
    """
    if _request_item(request).get_closest_marker("fresh_schema") is not None:
        worker = request.config.stash[_WORKER_ENGINES].get((make_url(db_preflight.url), db_schema))
        if worker is None or os.environ.get("DAIMON_TEST_FRESH_SCHEMA") == "1":
            async with _fresh_schema_session(db_preflight.url) as session:
                yield session
        else:
            async with _ddl_schema_session(worker, db_preflight.url) as session:
                yield session
        return
    async with db_engine.connect() as conn:
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await _drain_background_tasks()
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
