"""Real state-DB transactions behind the existing Gemini record/hold owners."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from daimon.core._models import GeminiRuntimeState
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini_state import PostgresFlashAccounting, PostgresGeminiStorage
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.ids import ModelRef, ResourceRef, Scope
from mux.contracts.resources import Artifact
from mux.drivers.gemini.flash import FLASH_PRIMARY, FlashAttempt
from mux.drivers.gemini.storage import ArtifactRecord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def postgres_state(db_nullpool_engine, db_clean):
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with sessions.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
        other = await make_account(db, tenant=tenant)
        foreign_tenant = await make_tenant(db)
        foreign = await make_account(db, tenant=foreign_tenant)
    scope = Scope(
        tenant_id=str(tenant.id),
        account_id=str(account.id),
        principal_id="caller",
        authorization_id="admitted",
    )
    return (
        sessions,
        scope,
        scope.model_copy(update={"account_id": str(other.id)}),
        scope.model_copy(
            update={"tenant_id": str(foreign_tenant.id), "account_id": str(foreign.id)}
        ),
    )


def holds(sessions, scope, *, project="project"):
    return PostgresFlashAccounting(
        sessions, scope=scope, project=project, hold_usd=Decimal(".49"), cap_usd=Decimal(".49")
    )


async def test_postgres_full_records_restart_and_rollback(postgres_state):
    sessions, scope, _, _ = postgres_state
    first = PostgresGeminiStorage(sessions, scope=scope, project="project")
    ref = ResourceRef(
        id="artifact",
        kind="artifact",
        provider="gemini",
        account_scope_id="project",
        tenant_id=scope.tenant_id,
        account_id=scope.account_id,
    )
    async with first.transaction() as records:
        records.creations[(scope.tenant_id, scope.account_id, "key")] = ("p", "digest", "agent")
        records.artifacts["artifact"] = ArtifactRecord(
            Artifact(
                ref=ref,
                filename="binary.bin",
                media_type="application/octet-stream",
                size_bytes=3,
                created_at=datetime.now(UTC),
            ),
            "digest",
            b"\x00\xff\x80",
        )
    with pytest.raises(RuntimeError):
        async with first.transaction() as records:
            records.creations.clear()
            raise RuntimeError("rollback")
    restarted = PostgresGeminiStorage(sessions, scope=scope, project="project")
    async with restarted.transaction() as records:
        assert len(records.creations) == 1
        assert records.artifacts["artifact"].body == b"\x00\xff\x80"


@pytest.mark.parametrize("existing", [False, True])
async def test_postgres_serializes_first_insert_and_releases_cancelled_writer(
    postgres_state, existing
):
    sessions, scope, _, _ = postgres_state
    first, second = (
        PostgresGeminiStorage(sessions, scope=scope, project="project") for _ in range(2)
    )
    if existing:
        async with first.transaction():
            pass
    entered = asyncio.Event()

    async def competing():
        async with second.transaction() as records:
            entered.set()
            records.creations[("t", "a", "two")] = ("p", "d", "two")

    async with first.transaction() as records:
        records.creations[("t", "a", "one")] = ("p", "d", "one")
        task = asyncio.create_task(competing())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(entered.wait(), timeout=0.1)
        assert not task.done()
    await asyncio.wait_for(task, timeout=5)
    with pytest.raises(asyncio.CancelledError):
        async with first.transaction() as records:
            records.creations.clear()
            raise asyncio.CancelledError
    async with second.transaction() as records:
        assert len(records.creations) == 2


@pytest.mark.parametrize("outcome", ["accepted", "refused", "unknown"])
async def test_postgres_concurrent_holds_and_receipts_survive_restart(postgres_state, outcome):
    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)

    async def attempt():
        owner = holds(sessions, scope)
        await owner.before_attempt(model=model, attempt=1)
        await owner.after_attempt(receipt=FlashAttempt(model, 1, outcome))

    result = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    assert sum(value is None for value in result) == 1
    assert sum(isinstance(value, AdmissionDenied) for value in result) == 1
    with pytest.raises(AdmissionDenied, match="channel_budget_exceeded"):
        await holds(sessions, scope).before_attempt(model=model, attempt=1)
    async with sessions() as db:
        bodies = (await db.scalars(select(GeminiRuntimeState.body))).all()
    assert len(bodies) == 1 and '"held_usd": "0.49"' in bodies[0]
    assert f'"outcome": "{outcome}"' in bodies[0]


async def test_postgres_hold_commits_inside_records_transaction_and_survives_rollback(
    postgres_state,
):
    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    storage = PostgresGeminiStorage(sessions, scope=scope, project="project")
    with pytest.raises(RuntimeError):
        async with storage.transaction() as records:
            records.creations[("t", "a", "pending")] = ("p", "d", "agent")
            await asyncio.wait_for(holds(sessions, scope).before_attempt(model=model, attempt=1), 5)
            # A second process sees the committed hold before any model send.
            with pytest.raises(AdmissionDenied):
                await holds(sessions, scope).before_attempt(model=model, attempt=1)
            raise RuntimeError("crash before native receipt")
    async with storage.transaction() as records:
        assert not records.creations
    with pytest.raises(AdmissionDenied):
        await holds(sessions, scope).before_attempt(model=model, attempt=1)


async def test_postgres_namespaces_isolate_tenant_account_and_project(postgres_state):
    sessions, scope, other, foreign = postgres_state
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    await holds(sessions, scope).before_attempt(model=model, attempt=1)
    async with PostgresGeminiStorage(
        sessions, scope=scope, project="project"
    ).transaction() as records:
        records.creations[("t", "a", "source")] = ("p", "d", "agent")
    for selected, project in ((other, "project"), (foreign, "project"), (scope, "other-project")):
        await holds(sessions, selected, project=project).before_attempt(model=model, attempt=1)
        async with PostgresGeminiStorage(
            sessions, scope=selected, project=project
        ).transaction() as records:
            assert not records.creations
    forged = scope.model_copy(update={"account_id": foreign.account_id})
    with pytest.raises(ValueError, match="account is unavailable"):
        await holds(sessions, forged).before_attempt(model=model, attempt=1)
    with pytest.raises(ValueError, match="account is unavailable"):
        async with PostgresGeminiStorage(
            sessions, scope=scope.model_copy(update={"account_id": str(uuid4())}), project="project"
        ).transaction():
            pytest.fail("unbound account admitted")


@pytest.mark.fresh_schema
async def test_postgres_state_migration_matches_models_and_refuses_state_loss(db_session):
    import importlib.util
    from pathlib import Path

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory
    from daimon.core._models import Base
    from sqlalchemy import inspect

    directory = Path(__file__).parents[2] / "alembic"
    spec = importlib.util.spec_from_file_location(
        "gemini_state_migration", directory / "versions/0106_gemini_runtime_state.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert ScriptDirectory(str(directory)).get_heads() == ["0106_gemini_runtime_state"]

    def check(connection):
        GeminiRuntimeState.__table__.drop(connection)
        before = set(inspect(connection).get_table_names())
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            assert set(inspect(connection).get_table_names()) == before | {"gemini_runtime_state"}
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
            assert not [diff for diff in differences if "gemini_runtime_state" in str(diff)]
            with pytest.raises(RuntimeError, match="unresolved financial holds"):
                module.downgrade()
            assert "gemini_runtime_state" in inspect(connection).get_table_names()

    connection = await db_session.connection()
    await connection.run_sync(check)


async def test_open_records_do_not_hold_the_financial_tenant_fence(postgres_state):
    from uuid import UUID

    from daimon.core.stores.tenant_ledger import lock_funds

    sessions, scope, _, _ = postgres_state

    async def financial_admission():
        async with sessions.begin() as db:
            assert await lock_funds(db, tenant_id=UUID(scope.tenant_id)) == UUID(scope.tenant_id)

    # The first row insert must finish its parent FK lock before records can
    # remain open across provider I/O, even with an independent hold in flight.
    async with PostgresGeminiStorage(sessions, scope=scope, project="new-project").transaction():
        await asyncio.wait_for(financial_admission(), timeout=1)


async def test_bounded_pool_contenders_release_connections_for_nested_holds(
    postgres_state, db_preflight, db_schema
):
    from daimon.core.turn.errors import SessionBusyError
    from daimon.testing.db import build_test_engine
    from sqlalchemy import text

    _, scope, _, _ = postgres_state
    engine = build_test_engine(
        db_preflight.url, db_schema, pool_size=2, max_overflow=0, pool_timeout=0.5
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    storage = PostgresGeminiStorage(sessions, scope=scope, project="contended")

    async def contend():
        async with PostgresGeminiStorage(sessions, scope=scope, project="contended").transaction():
            pytest.fail("contender passed an active writer")

    tasks = []
    try:
        async with storage.transaction():
            tasks = [asyncio.create_task(contend()) for _ in range(8)]
            await asyncio.sleep(0.15)
            await asyncio.wait_for(
                holds(sessions, scope, project="independent").before_attempt(
                    model=ModelRef(provider="gemini", id=FLASH_PRIMARY), attempt=1
                ),
                timeout=2,
            )
            async with sessions() as db:
                assert await asyncio.wait_for(db.scalar(text("SELECT 1")), 2) == 1
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 7)
            assert all(isinstance(value, SessionBusyError) for value in results)
        assert engine.pool.checkedout() == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()


async def test_open_records_do_not_block_account_update_fence(postgres_state):
    from uuid import UUID

    from daimon.core._models import Account

    sessions, scope, _, _ = postgres_state
    async with (
        PostgresGeminiStorage(sessions, scope=scope, project="project").transaction(),
        sessions.begin() as db,
    ):
        assert await db.scalar(
            select(Account.id)
            .where(Account.id == UUID(scope.account_id))
            .with_for_update(nowait=True)
        ) == UUID(scope.account_id)


async def test_post_dispatch_waiter_keeps_short_polls_and_pool_headroom(
    postgres_state, db_preflight, db_schema, monkeypatch
):
    from daimon.core import session_fence_retry
    from daimon.testing.db import build_test_engine

    _, scope, _, _ = postgres_state
    engine = build_test_engine(
        db_preflight.url, db_schema, pool_size=2, max_overflow=0, pool_timeout=0.5
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    storage = PostgresGeminiStorage(sessions, scope=scope, project="polling")
    original_sleep = asyncio.sleep
    polling = asyncio.Event()
    sleeps = []
    waiter = None

    async def observed_sleep(delay):
        if asyncio.current_task() is waiter:
            sleeps.append(delay)
            if len(sleeps) == 8:
                polling.set()
        await original_sleep(delay)

    async def resolve():
        async with storage.transaction(after_dispatch=True) as records:
            records.creations[("t", "a", "resolved")] = ("p", "d", "agent")

    monkeypatch.setattr(session_fence_retry.random, "uniform", lambda low, high: high)
    monkeypatch.setattr(session_fence_retry.asyncio, "sleep", observed_sleep)
    try:
        async with storage.transaction():
            waiter = asyncio.create_task(resolve())
            await asyncio.wait_for(polling.wait(), 7)
            # The eighth retry must still release the second pool connection;
            # nested durable holds retain capacity while Records is locked.
            assert engine.pool.checkedout() == 1
            await asyncio.wait_for(
                holds(sessions, scope, project="polling").before_attempt(
                    model=ModelRef(provider="gemini", id=FLASH_PRIMARY), attempt=1
                ),
                0.5,
            )
        # An old waiter must not sleep for a second while new contenders start
        # at the short cadence. Use the longest jitter for a stable regression.
        await asyncio.wait_for(waiter, 0.5)
        assert len(sleeps) >= 8 and max(sleeps) <= 0.075
        async with storage.transaction() as records:
            assert ("t", "a", "resolved") in records.creations
        assert engine.pool.checkedout() == 0
    finally:
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await engine.dispose()


async def test_post_dispatch_acquisition_respects_an_earlier_outer_send_timeout(postgres_state):
    sessions, scope, _, _ = postgres_state
    storage = PostgresGeminiStorage(sessions, scope=scope, project="outer-timeout")
    async with storage.transaction():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1), storage.transaction(after_dispatch=True):
                pytest.fail("post-dispatch writer passed an active holder")
    # Cancellation of the long acquisition must not leak a connection or lock.
    async with asyncio.timeout(1), storage.transaction(after_dispatch=True):
        pass


@pytest.mark.parametrize("kind", ["records", "holds"])
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_pg_commit_finishes_before_cancellation_propagates(
    postgres_state, monkeypatch, kind, cancellations
):
    from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction
    from sqlalchemy.sql.dml import Update

    original_sessions, scope, _, _ = postgres_state
    started, release = asyncio.Event(), asyncio.Event()

    class ObservedSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if isinstance(statement, Update) and statement.table.name == "gemini_runtime_state":
                self.info["payload_updated"] = True
            return result

    sessions = async_sessionmaker(
        original_sessions.kw["bind"], class_=ObservedSession, expire_on_commit=False
    )
    original = AsyncSessionTransaction.commit
    paused = False

    async def delayed_commit(transaction):
        nonlocal paused
        if transaction.session.info.get("payload_updated") and not paused:
            paused = True
            started.set()
            await release.wait()
        await original(transaction)

    monkeypatch.setattr(AsyncSessionTransaction, "commit", delayed_commit)

    async def writer():
        if kind == "holds":
            await holds(sessions, scope).before_attempt(
                model=ModelRef(provider="gemini", id=FLASH_PRIMARY), attempt=1
            )
        else:
            async with PostgresGeminiStorage(
                sessions, scope=scope, project="project"
            ).transaction() as records:
                records.creations[("t", "a", "committed")] = ("p", "d", "agent")

    task = asyncio.create_task(writer())
    try:
        await asyncio.wait_for(started.wait(), 3)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if kind == "holds":
        with pytest.raises(AdmissionDenied):
            await holds(sessions, scope).before_attempt(
                model=ModelRef(provider="gemini", id=FLASH_PRIMARY), attempt=1
            )
    else:
        async with PostgresGeminiStorage(
            sessions, scope=scope, project="project"
        ).transaction() as records:
            assert records.creations[("t", "a", "committed")] == ("p", "d", "agent")


async def test_read_only_records_do_not_rewrite_payload(postgres_state):
    from sqlalchemy import event

    sessions, scope, _, _ = postgres_state
    storage = PostgresGeminiStorage(sessions, scope=scope, project="project")
    async with storage.transaction() as records:
        records.creations[("t", "a", "key")] = ("p", "d", "agent")
    updates = []

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE gemini_runtime_state"):
            updates.append(statement)

    engine = sessions.kw["bind"]
    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        async with storage.transaction() as records:
            assert len(records.creations) == 1
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
    assert updates == []


async def test_account_purge_previews_and_deletes_records_but_retains_financial_holds(
    postgres_state,
):
    from uuid import UUID

    from daimon.core.privacy import collect_purge_preview
    from daimon.core.purge import purge_account

    sessions, scope, other, _ = postgres_state
    for selected in (scope, other):
        async with PostgresGeminiStorage(
            sessions, scope=selected, project="project"
        ).transaction() as records:
            records.creations[("t", "a", "private")] = ("p", "d", "agent")
        await holds(sessions, selected).before_attempt(
            model=ModelRef(provider="gemini", id=FLASH_PRIMARY), attempt=1
        )
    preview = await collect_purge_preview(sm=sessions, account_id=UUID(scope.account_id))
    assert preview.gemini_records.count == 1
    result = await purge_account(sm=sessions, account_id=UUID(scope.account_id))
    assert result.db.gemini_records == preview.gemini_records.count
    async with sessions() as db:
        rows = (
            await db.execute(select(GeminiRuntimeState.account_id, GeminiRuntimeState.kind))
        ).all()
    assert (UUID(scope.account_id), "records") not in rows
    assert (UUID(scope.account_id), "holds") in rows
    assert (UUID(other.account_id), "records") in rows


@pytest.mark.parametrize("refused", [False, True])
async def test_post_dispatch_outcome_waits_past_busy_bound_without_wedging_session(
    postgres_state, refused
):
    from daimon.testing.drivers.gemini.fake import FakeTransport
    from mux.contracts.actions import UserMessage
    from mux.contracts.events import TextPart
    from mux.contracts.extensions import ExtensionConfig
    from mux.contracts.ids import ChannelRef, ThreadRef
    from mux.contracts.resources import AgentSpec, EnvironmentSpec, SessionSpec
    from mux.drivers.gemini import GeminiManagedAgents
    from mux.errors import ProviderError
    from mux.state.memory import MemoryStateStore

    from .test_gemini_codec import interaction

    sessions, scope, _, _ = postgres_state
    dispatched, release_response = asyncio.Event(), asyncio.Event()

    class PausedResponse(FakeTransport):
        async def create(self, request):
            try:
                return await super().create(request)
            finally:
                dispatched.set()
                await release_response.wait()

    transport, state = PausedResponse(), MemoryStateStore()
    storage = PostgresGeminiStorage(sessions, scope=scope, project="project")
    backend = GeminiManagedAgents(
        transport, storage=storage, state_store=state, account_scope_id="project"
    )
    agent = await backend.agents.create(
        scope,
        AgentSpec(name="inline", model=ModelRef(provider="gemini", id=FLASH_PRIMARY)),
        key="agent",
    )
    environment = await backend.environments.create(
        scope, EnvironmentSpec(name="inline"), key="env"
    )
    thread = ThreadRef(
        channel=ChannelRef(tenant_id=scope.tenant_id, platform="test", channel_id="channel"),
        thread_id="thread",
    )
    session = await backend.sessions.create(
        scope,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=environment.ref,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={"binding_id": "binding", "thread": thread.model_dump(mode="json")},
                )
            },
        ),
        key="session",
    )
    failure = ProviderError("permission", retryable=False)
    transport.responses.append(failure if refused else interaction("accepted"))
    message = (UserMessage(content=(TextPart(text="hello"),)),)
    task = asyncio.create_task(backend.events.send(scope, session.ref, message, key="first"))
    try:
        await asyncio.wait_for(dispatched.wait(), 3)
        # The POST has completed, but another session's provider I/O holds the
        # account/project Records row while this outcome needs to be committed.
        async with storage.transaction() as records:
            assert records.sessions[session.ref.id].unknown_delivery
            release_response.set()
            await asyncio.sleep(5.5)
            assert not task.done()
        if refused:
            with pytest.raises(ProviderError) as error:
                await asyncio.wait_for(task, 3)
            assert error.value is failure
        else:
            receipt = await asyncio.wait_for(task, 3)
            assert receipt.status == "queued"
            assert await backend.events.send(scope, session.ref, message, key="first") == receipt
        async with storage.transaction() as records:
            assert not records.sessions[session.ref.id].unknown_delivery
        operation = await state.get_operation(scope, "first")
        assert operation is not None
        assert operation.operation.status == ("failed" if refused else "accepted")
        # Both a definite refusal and a persisted acknowledgement permit a new
        # operation, without re-POSTing the accepted idempotency key.
        transport.responses.append(interaction("next"))
        assert (
            await backend.events.send(scope, session.ref, message, key="second")
        ).status == "queued"
        assert len(transport.requests) == 2
    finally:
        release_response.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
