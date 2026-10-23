"""Tests for daimon.testing.db: the per-worker schema fixture graph.

These tests require an external Postgres instance with a migrated daimon_test
database. Set DAIMON_DATABASE__TEST_URL=postgresql+asyncpg://... before running.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid

import pytest
from daimon.core import agent_identity
from daimon.core._models import Base
from daimon.testing.db import (
    _WORKER_ENGINES,  # pyright: ignore[reportPrivateUsage]
    _assert_committing_pool_clean,  # pyright: ignore[reportPrivateUsage]
    _create_schema_with_tables,  # pyright: ignore[reportPrivateUsage]
    _drain_background_tasks,  # pyright: ignore[reportPrivateUsage]
    _drop_schema,  # pyright: ignore[reportPrivateUsage]
    _forget_touched_connections,  # pyright: ignore[reportPrivateUsage]
    _fresh_schema_session,  # pyright: ignore[reportPrivateUsage]
    _raise_on_leaked_transactions,  # pyright: ignore[reportPrivateUsage]
    _ready_ddl_schema,  # pyright: ignore[reportPrivateUsage]
    _require_test_dsn,  # pyright: ignore[reportPrivateUsage]
    _schema_fingerprint,  # pyright: ignore[reportPrivateUsage]
    build_test_engine,
    sibling_nullpool_engine,
    sweep_orphan_schemas,
    truncate_all,
    wipe_schema,
)
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
)
from sqlalchemy.pool import NullPool, QueuePool

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


async def test_worker_connections_give_up_on_a_lock_wait_after_a_minute(
    db_session: AsyncSession,
) -> None:
    """A lock wait between a test's own connections must end, not hang the job."""
    assert (await db_session.execute(text("SHOW lock_timeout"))).scalar_one() == "1min"


_SESSION_LEAKS = [
    pytest.param("SET search_path = public", "search_path is 'public'", id="set-search-path"),
    pytest.param(
        "SET statement_timeout = '5s'",
        "statement_timeout is '5s'",
        id="set-statement-timeout",
    ),
    pytest.param("SET TIME ZONE 'Asia/Manila'", "TimeZone is 'Asia/Manila'", id="set-timezone"),
    pytest.param("CREATE TEMP TABLE leaked_temp (x integer)", "1 temp object", id="temp-table"),
    pytest.param("LISTEN leaked_channel", "LISTEN channel", id="listen"),
    pytest.param(
        "SELECT pg_advisory_lock(7310042)", "session-level advisory lock", id="advisory-lock"
    ),
]


@pytest.mark.parametrize(("statement", "message"), _SESSION_LEAKS)
async def test_committing_pool_guard_rejects_session_state_left_on_a_pooled_connection(
    db_committing_pool: AsyncEngine, db_schema: str, statement: str, message: str
) -> None:
    """What a test leaves on a pooled connection would reach the next test; the guard names it."""
    pool = db_committing_pool.pool
    if not isinstance(pool, QueuePool):
        pytest.skip("DAIMON_TEST_NULLPOOL=1 closes every connection on checkin")
    baseline = pool.checkedout()
    _forget_touched_connections(pool)
    async with db_committing_pool.connect() as conn:
        await conn.execute(text(statement))
        await conn.commit()  # the session survives the commit; only the pool can end it
    with pytest.raises(AssertionError, match=message):
        await _assert_committing_pool_clean(db_committing_pool, pool, baseline, db_schema)
    # The guard repaired the connection, so the next test starts clean.
    async with db_committing_pool.connect() as conn:
        await conn.execute(text("SELECT 1"))
    await _assert_committing_pool_clean(db_committing_pool, pool, baseline, db_schema)


async def test_committing_pool_guard_rejects_a_transaction_left_open_on_a_pooled_connection(
    db_committing_pool: AsyncEngine, db_schema: str
) -> None:
    pool = db_committing_pool.pool
    if not isinstance(pool, QueuePool):
        pytest.skip("DAIMON_TEST_NULLPOOL=1 keeps no checkout count")
    baseline = pool.checkedout()
    conn = await db_committing_pool.connect()
    try:
        await conn.execute(text("SELECT 1"))  # begins a transaction nobody ends
        with pytest.raises(AssertionError, match="still checked out"):
            await _assert_committing_pool_clean(db_committing_pool, pool, baseline, db_schema)
    finally:
        await conn.close()
    await _assert_committing_pool_clean(db_committing_pool, pool, baseline, db_schema)


async def test_committing_pool_guard_rejects_a_session_fence_permit_never_released(
    db_committing_pool: AsyncEngine, db_schema: str
) -> None:
    from daimon.core.session_preparation_gate import pool_headroom

    pool = db_committing_pool.pool
    if not isinstance(pool, QueuePool):
        pytest.skip("DAIMON_TEST_NULLPOOL=1 has no session fence")
    sessions = async_sessionmaker(db_committing_pool, expire_on_commit=False)
    async with pool_headroom(sessions):
        with pytest.raises(AssertionError, match="fence permit"):
            await _assert_committing_pool_clean(db_committing_pool, pool, 0, db_schema)
    await _assert_committing_pool_clean(db_committing_pool, pool, 0, db_schema)


@pytest.mark.fresh_schema
async def test_ddl_schema_is_rebuilt_after_a_test_commits_ddl_or_rows(
    db_session: AsyncSession,
    db_preflight: object,
    db_schema: str,
    request: pytest.FixtureRequest,
) -> None:
    """A fresh_schema test that commits leaves the next one a clean, ORM-shaped schema."""
    if os.environ.get("DAIMON_TEST_FRESH_SCHEMA") == "1":
        pytest.skip("DAIMON_TEST_FRESH_SCHEMA=1 gives every fresh_schema test a private schema")
    url = os.environ["DAIMON_DATABASE__TEST_URL"]
    worker = request.config.stash[_WORKER_ENGINES][(make_url(url), db_schema)]
    assert worker.ddl is not None
    ddl = worker.ddl
    async with ddl.engine.begin() as conn:  # committed behind the session's back
        await conn.execute(text("ALTER TABLE tenants ADD COLUMN leaked integer"))
        await conn.execute(
            text(
                "CREATE FUNCTION leaked_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER leaked_trg BEFORE INSERT ON tenants FOR EACH ROW EXECUTE FUNCTION leaked_fn()"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO tenants (id, platform, external_id) VALUES (gen_random_uuid(), 'd', 'x')"
            )
        )
    await db_session.rollback()
    again = await _ready_ddl_schema(worker, url)
    assert again.name == ddl.name
    async with ddl.engine.connect() as conn:
        columns = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = 'tenants' AND column_name = 'leaked'"
                ),
                {"s": ddl.name},
            )
        ).scalar_one()
        rows = (await conn.execute(text("SELECT count(*) FROM tenants"))).scalar_one()
        functions = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = :s AND p.proname = 'leaked_fn'"
                ),
                {"s": ddl.name},
            )
        ).scalar_one()
    assert (columns, rows, functions) == (0, 0, 0)


@pytest.mark.fresh_schema
async def test_ddl_schema_restarts_sequences_an_earlier_test_advanced(
    db_session: AsyncSession,
    db_preflight: object,
    db_schema: str,
    request: pytest.FixtureRequest,
) -> None:
    """Rolled-back inserts still consume ids; a fresh_schema test must start at 1 again."""
    if os.environ.get("DAIMON_TEST_FRESH_SCHEMA") == "1":
        pytest.skip("DAIMON_TEST_FRESH_SCHEMA=1 gives every fresh_schema test a private schema")
    url = os.environ["DAIMON_DATABASE__TEST_URL"]
    worker = request.config.stash[_WORKER_ENGINES][(make_url(url), db_schema)]
    assert worker.ddl is not None
    ddl = worker.ddl
    async with ddl.engine.connect() as conn:
        sequence = (
            await conn.execute(
                text("SELECT sequencename FROM pg_sequences WHERE schemaname = :s LIMIT 1"),
                {"s": ddl.name},
            )
        ).scalar_one()
        await conn.execute(text(f'SELECT nextval(\'"{ddl.name}"."{sequence}"\')'))
        await conn.rollback()  # nextval is not transactional: the sequence stays advanced
    await db_session.rollback()
    await _ready_ddl_schema(worker, url)
    async with ddl.engine.connect() as conn:
        last = (
            await conn.execute(
                text(
                    "SELECT last_value FROM pg_sequences WHERE schemaname = :s AND sequencename = :n"
                ),
                {"s": ddl.name, "n": sequence},
            )
        ).scalar_one()
    assert last is None, "the sequence must be back to its starting point"


_COMMITTED_DDL = [
    pytest.param("ALTER TABLE tenants ADD COLUMN leaked integer", id="column"),
    pytest.param(
        "ALTER TABLE tenants ADD COLUMN leaked integer GENERATED ALWAYS AS IDENTITY", id="identity"
    ),
    pytest.param("CREATE INDEX leaked_idx ON tenants (id)", id="index"),
    pytest.param("ALTER TABLE tenants ADD CONSTRAINT leaked_ck CHECK (true)", id="constraint"),
    pytest.param("CREATE VIEW leaked_view AS SELECT 1 AS x", id="view"),
    pytest.param("CREATE DOMAIN leaked_domain AS integer", id="domain"),
    pytest.param("CREATE TYPE leaked_enum AS ENUM ('a')", id="enum"),
    pytest.param(
        "CREATE FUNCTION leaked_fn() RETURNS integer LANGUAGE sql AS 'SELECT 1'", id="function"
    ),
    pytest.param("CREATE SEQUENCE leaked_seq", id="sequence"),
]


@pytest.mark.fresh_schema
@pytest.mark.parametrize("statement", _COMMITTED_DDL)
async def test_ddl_schema_fingerprint_notices_committed_ddl_of_every_kind(
    db_session: AsyncSession,
    db_preflight: object,
    db_schema: str,
    request: pytest.FixtureRequest,
    statement: str,
) -> None:
    """Every kind of committed DDL changes the fingerprint, so the next test gets a rebuild."""
    if os.environ.get("DAIMON_TEST_FRESH_SCHEMA") == "1":
        pytest.skip("DAIMON_TEST_FRESH_SCHEMA=1 gives every fresh_schema test a private schema")
    url = os.environ["DAIMON_DATABASE__TEST_URL"]
    worker = request.config.stash[_WORKER_ENGINES][(make_url(url), db_schema)]
    assert worker.ddl is not None
    ddl = worker.ddl
    assert await _schema_fingerprint(ddl.engine, ddl.name) == ddl.baseline
    async with ddl.engine.begin() as conn:
        await conn.execute(text(statement))
    assert await _schema_fingerprint(ddl.engine, ddl.name) != ddl.baseline
    await db_session.rollback()
    await _ready_ddl_schema(worker, url)
    assert await _schema_fingerprint(ddl.engine, ddl.name) == ddl.baseline


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


@pytest.mark.parametrize("attempts", [1, 3], ids=["no-retry", "retry"])
async def test_wipe_schema_retries_when_it_loses_a_deadlock_to_a_late_writer(
    db_engine: AsyncEngine,
    db_schema: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    attempts: int,
) -> None:
    """A write an earlier test left running deadlocks with the next test's wipe.

    The late writer holds FOR KEY SHARE on the second tenant (as an FK check
    does) while the wipe, having deleted the first tenant, waits on it; the
    writer then waits on the first tenant. Postgres aborts the wipe, the first
    waiter to check. One attempt fails the setup (the CI flake); a retry wipes.
    """
    import daimon.testing.db as db_module
    from sqlalchemy.exc import DBAPIError

    monkeypatch.setattr(db_module, "_WIPE_ATTEMPTS", attempts)
    first = await make_tenant(db_session)
    second = await make_tenant(db_session)
    await db_session.commit()
    tenants = f'"{db_schema}".tenants'
    late = sibling_nullpool_engine(db_engine)
    try:
        async with late.connect() as writer:
            await writer.execute(
                text(f"SELECT 1 FROM {tenants} WHERE id = :id FOR KEY SHARE"), {"id": second.id}
            )
            wipe = asyncio.create_task(wipe_schema(db_engine, db_schema, lock_timeout="10s"))
            await asyncio.sleep(0.3)  # the wipe has deleted the first tenant, waits on the second
            await writer.execute(
                text(f"UPDATE {tenants} SET id = id WHERE id = :id"), {"id": first.id}
            )
            await writer.commit()
            if attempts == 1:
                with pytest.raises(DBAPIError, match="deadlock"):
                    await wipe
                return
            await wipe
    finally:
        await late.dispose()
    async with db_engine.connect() as fresh:
        count = (await fresh.execute(text(f"SELECT count(*) FROM {tenants}"))).scalar_one()
    assert count == 0


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


async def test_background_face_transaction_finishes_before_worker_schema_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued face must release its Postgres locks before schema teardown."""
    schema = f"test_f{os.getpid()}_{uuid.uuid4().hex[:8]}"
    engine = build_test_engine(os.environ["DAIMON_DATABASE__TEST_URL"], schema, poolclass=NullPool)
    started = asyncio.Event()
    rollback_started = asyncio.Event()
    release_rollback = asyncio.Event()
    original_exit = AsyncSessionTransaction.__aexit__

    async def delayed_exit(self: AsyncSessionTransaction, *args: object) -> None:
        rollback_started.set()
        await release_rollback.wait()
        await original_exit(self, *args)

    async def blocked_face(session: AsyncSession, **_kwargs: object) -> None:
        await session.execute(text("SELECT id FROM tenants FOR KEY SHARE"))
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(agent_identity, "get_or_create_avatar", blocked_face)
    task: asyncio.Task[None] | None = None
    drain: asyncio.Task[None] | None = None
    try:
        await _create_schema_with_tables(engine, schema)
        monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", delayed_exit)
        task = agent_identity.queue_agent_face(
            async_sessionmaker(engine),
            tenant_id=uuid.uuid4(),
            agent_name="teardown-race",
            metadata=None,
            default_agent_name=None,
        )
        assert task is not None
        await asyncio.wait_for(started.wait(), timeout=2)
        # Existing suite-local autouse fixtures may request cancellation first.
        # The shared DB cleanup must still see and await that task's rollback.
        agent_identity.cancel_pending_agent_faces()
        await asyncio.wait_for(rollback_started.wait(), timeout=2)
        drain = asyncio.create_task(_drain_background_tasks())
        await asyncio.sleep(0)
        assert task.cancelling() == 1, "cleanup must not interrupt an in-flight rollback"
        assert not drain.done(), "cleanup must wait for the shielded SQLAlchemy rollback"
        release_rollback.set()
        await asyncio.wait_for(drain, timeout=2)
        assert task.done(), "cleanup must await cancellation and transaction rollback"
        await asyncio.wait_for(_drop_schema(engine, schema), timeout=10)
    finally:
        release_rollback.set()
        if drain is not None and not drain.done():
            await asyncio.gather(drain, return_exceptions=True)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def test_background_face_transaction_finishes_before_fresh_schema_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh-schema test must drain its face before its private schema drops."""
    started = asyncio.Event()

    async def blocked_face(session: AsyncSession, **_kwargs: object) -> None:
        await session.execute(text("SELECT id FROM tenants FOR KEY SHARE"))
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(agent_identity, "get_or_create_avatar", blocked_face)
    task: asyncio.Task[None] | None = None
    schema: str | None = None
    fresh = _fresh_schema_session(os.environ["DAIMON_DATABASE__TEST_URL"])
    try:
        session = await fresh.__aenter__()
        schema = (await session.execute(text("SELECT current_schema()"))).scalar_one()
        task = agent_identity.queue_agent_face(
            async_sessionmaker(session.bind),
            tenant_id=uuid.uuid4(),
            agent_name="fresh-teardown-race",
            metadata=None,
            default_agent_name=None,
        )
        assert task is not None
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(fresh.__aexit__(None, None, None), timeout=10)
        assert task is not None and task.done()
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if schema is not None:
            cleanup = build_test_engine(
                os.environ["DAIMON_DATABASE__TEST_URL"], schema, poolclass=NullPool
            )
            try:
                async with cleanup.begin() as conn:
                    await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            finally:
                await cleanup.dispose()


@pytest.mark.asyncio
async def test_a_connection_returned_mid_transaction_is_closed_and_its_test_named(
    db_engine: AsyncEngine,
) -> None:
    """A pool taking a connection back inside a transaction fails the leaking test at once.

    ``BEGIN`` issued on the driver connection is a transaction SQLAlchemy does not
    track, so the pool's rollback-on-return skips it, as in a hung CI run where
    one pooled connection sat ``idle in transaction`` holding row locks until the
    sibling connection's wipe timed out. The guard terminates that backend and the
    teardown check names the test.
    """
    nodeid = "packages/example/tests/test_leaker.py::test_leaks"
    async with db_engine.connect() as conn:
        driver = (await conn.get_raw_connection()).driver_connection
        assert driver is not None
        leaked_pid: int = driver.get_server_pid()
        await driver.execute("BEGIN")

    with pytest.raises(AssertionError, match=re.escape(nodeid)) as caught:
        _raise_on_leaked_transactions(db_engine.pool, nodeid)
    assert f"backend pid {leaked_pid}" in str(caught.value), (
        "the failure must say which backend was returned inside a transaction"
    )
    # The report is delivered once, and the leaked backend is not handed out again.
    _raise_on_leaked_transactions(db_engine.pool, nodeid)
    async with db_engine.connect() as conn:
        next_driver = (await conn.get_raw_connection()).driver_connection
        assert next_driver is not None
        assert next_driver.get_server_pid() != leaked_pid, (
            "the pool must not hand the transaction-holding backend out again"
        )
        assert not next_driver.is_in_transaction()


@pytest.mark.asyncio
async def test_a_connection_returned_clean_is_not_reported(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    _raise_on_leaked_transactions(db_engine.pool, "packages/example/tests/test_clean.py::test_ok")


@pytest.mark.asyncio
async def test_rolling_back_a_shared_connection_waits_for_the_statement_running_on_it(
    db_engine: AsyncEngine,
) -> None:
    """A teardown ``rollback`` no longer races a background task's statement.

    Two tasks on one connection used to interleave their COMMIT / ROLLBACK with
    each other's statements until SQLAlchemy forgot a transaction asyncpg still
    held (see ``_end_transactions_under_the_statement_mutex``). Without the fix,
    the rollback below raises ``InterfaceError: another operation is in progress``
    and the connection goes back to the pool with its transaction open.
    """
    async with db_engine.connect() as conn:
        driver = (await conn.get_raw_connection()).driver_connection
        assert driver is not None
        await conn.execute(text("SELECT 1"))
        background = asyncio.create_task(conn.execute(text("SELECT pg_sleep(0.3)")))
        await asyncio.sleep(0.05)
        await conn.rollback()
        await background
        await conn.execute(text("SELECT 2"))
        await conn.rollback()
    assert not driver.is_in_transaction(), (
        "the pool must get the connection back outside a transaction"
    )
    assert driver._top_xact is None, "asyncpg must not still hold a transaction SQLAlchemy dropped"  # pyright: ignore[reportPrivateUsage]
    _raise_on_leaked_transactions(db_engine.pool, "packages/testing/tests/test_db.py")
