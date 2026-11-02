"""Real state-DB transactions behind the existing Gemini record/hold owners."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from daimon.core._models import GeminiRuntimeState
from daimon.core.stores import gemini_runtime_state
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
async def postgres_state(db_committing_engine, db_clean):
    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
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


@pytest.mark.parametrize(
    ("outcome", "status", "released"),
    [
        ("refused", 400, True),  # Google rejected the request: nothing to bill
        ("refused", 404, True),
        ("refused", 503, True),  # a 503 refused the work: no interaction returned
        ("refused", None, False),
        ("unknown", None, False),  # maybe accepted: the full hold stays
    ],
)
async def test_postgres_definite_refusal_releases_its_hold(
    postgres_state, outcome, status, released
):
    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    owner = holds(sessions, scope)
    await owner.before_attempt(model=model, attempt=1)
    await owner.after_attempt(receipt=FlashAttempt(model, 1, outcome, http_status=status))
    async with sessions() as db:
        (body,) = (await db.scalars(select(GeminiRuntimeState.body))).all()
    (row,) = json.loads(body).values()
    assert row["outcome"] == outcome and row["http_status"] == status
    if released:
        assert (row["held_usd"], row["actual_usd"], row["released_usd"]) == ("0", "0", "0.49")
        # The budget is free again for the next attempt (cap == one hold).
        await holds(sessions, scope).before_attempt(model=model, attempt=1)
    else:
        assert row["held_usd"] == "0.49" and "released_usd" not in row
        with pytest.raises(AdmissionDenied, match="provider_hold_cap_reached"):
            await holds(sessions, scope).before_attempt(model=model, attempt=1)


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
    with pytest.raises(AdmissionDenied, match="provider_hold_cap_reached"):
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
    script = ScriptDirectory(str(directory))
    [head] = script.get_heads()
    chain = {revision.revision for revision in script.iterate_revisions(head, "base")}
    assert "0106_gemini_runtime_state" in chain, "the migration is on the line to the head"

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


# The waiter's poll cadence and the pool headroom are asserted on counted events, not on
# elapsed time. These bounds only end a hung run, so they sit far above the unloaded cost: the
# holds reservation is ~25 serial database round trips, and it took 0.5s on a CI runner whose
# four vCPUs were shared with four pytest workers and their Postgres, which failed a 0.5s bound.
HUNG_RUN_BOUND_S = 30
# A checkout waits only for the waiter's one in-flight attempt (~10 round trips). A pool
# timeout is retried like any busy fence, so a pool the waiter exhausts shows up as the
# headroom assertion below within seconds, not as a hang.
POOL_WAIT_S = 1


async def _within_hung_run_bound(awaitable, what: str):
    try:
        return await asyncio.wait_for(awaitable, HUNG_RUN_BOUND_S)
    except TimeoutError:
        pytest.fail(f"{what} did not finish within {HUNG_RUN_BOUND_S}s (hung, not slow)")


async def test_post_dispatch_waiter_keeps_short_polls_and_pool_headroom(
    postgres_state, db_preflight, db_schema, monkeypatch
):
    from daimon.core import session_fence_retry
    from daimon.testing.db import build_test_engine

    _, scope, _, _ = postgres_state
    engine = build_test_engine(
        db_preflight.url, db_schema, pool_size=2, max_overflow=0, pool_timeout=POOL_WAIT_S
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

    async def eighth_retry_or_waiter_exit():
        """End at the eighth retry, or at the waiter's own exit so its error is reported."""
        eighth = asyncio.create_task(polling.wait())
        try:
            await asyncio.wait({eighth, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            eighth.cancel()
        assert polling.is_set(), f"the waiter ended after {len(sleeps)} retries: {waiter!r}"

    monkeypatch.setattr(session_fence_retry.random, "uniform", lambda low, high: high)
    monkeypatch.setattr(session_fence_retry.asyncio, "sleep", observed_sleep)
    try:
        async with storage.transaction():
            waiter = asyncio.create_task(resolve())
            await _within_hung_run_bound(eighth_retry_or_waiter_exit(), "the eighth retry")
            # The eighth retry must still release the second pool connection;
            # nested durable holds retain capacity while Records is locked.
            assert engine.pool.checkedout() == 1, (
                "the waiter kept its pool connection while sleeping between retries"
            )
            await _within_hung_run_bound(
                holds(sessions, scope, project="polling").before_attempt(
                    model=ModelRef(provider="gemini", id=FLASH_PRIMARY), attempt=1
                ),
                "a holds reservation while a post-dispatch waiter polls",
            )
        # An old waiter must not sleep for a second while new contenders start
        # at the short cadence. Use the longest jitter for a stable regression.
        await _within_hung_run_bound(waiter, "the waiter after Records unlocked")
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
    postgres_state, refused, monkeypatch
):
    from mux.contracts.actions import UserMessage
    from mux.contracts.events import TextPart
    from mux.contracts.extensions import ExtensionConfig
    from mux.contracts.ids import ChannelRef, ThreadRef
    from mux.contracts.resources import AgentSpec, EnvironmentSpec, SessionSpec
    from mux.drivers.gemini import GeminiManagedAgents
    from mux.errors import ProviderError
    from mux.state.memory import MemoryStateStore
    from mux.testing.drivers.gemini.fake import FakeTransport

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
        # Shrink the pre-dispatch busy bound and hold the row twice as long,
        # rather than outlasting the production 5 s.
        monkeypatch.setattr(gemini_runtime_state, "PRE_DISPATCH_BUSY_BOUND_S", 0.3)
        async with storage.transaction() as records:
            assert records.sessions[session.ref.id].unknown_delivery
            release_response.set()
            await asyncio.sleep(0.6)
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


async def _gemini_session(storage, scope):
    """A real Gemini driver on `storage`, with one session that ran one turn (i1)."""
    from mux.contracts.actions import UserMessage
    from mux.contracts.events import TextPart
    from mux.contracts.extensions import ExtensionConfig
    from mux.contracts.ids import ChannelRef, ThreadRef
    from mux.contracts.resources import AgentSpec, EnvironmentSpec, SessionSpec
    from mux.drivers.gemini import GeminiManagedAgents
    from mux.state.memory import MemoryStateStore
    from mux.testing.drivers.gemini.fake import FakeTransport

    transport = FakeTransport()
    driver = GeminiManagedAgents(
        transport, storage=storage, state_store=MemoryStateStore(), account_scope_id="project"
    )
    agent = await driver.agents.create(
        scope,
        AgentSpec(name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        key="agent",
    )
    env = await driver.environments.create(scope, EnvironmentSpec(name="e"), key="env")
    thread = ThreadRef(
        channel=ChannelRef(tenant_id=scope.tenant_id, platform="discord", channel_id="c"),
        thread_id="th",
    )
    spec = SessionSpec(
        agent=agent.ref,
        agent_revision=agent.revision,
        environment=env.ref,
        config_revision=1,
        extensions={
            "gemini.session": ExtensionConfig(
                namespace="gemini.session",
                version=1,
                value={"thread": thread.model_dump(mode="json"), "binding_id": "b"},
            )
        },
    )
    session = await driver.sessions.create(scope, spec, key="session")
    stamp = datetime.now(UTC).isoformat()
    transport.responses.append(
        {
            "id": "i1",
            "status": "completed",
            "created": stamp,
            "updated": stamp,
            "environment_id": "e1",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": "done"}]}],
            "usage": {
                "total_input_tokens": 10,
                "total_output_tokens": 20,
                "total_thought_tokens": 0,
            },
        }
    )
    await driver.events.send(
        scope, session.ref, (UserMessage(content=(TextPart(text="hi"),)),), key="send"
    )
    return driver, transport, session


async def test_usage_and_event_listing_never_wait_behind_the_accounts_records_writer(
    postgres_state,
):
    """Smokes a1d991fb/dce44708/7da21ee3 (J2 1YK993, J6g PAKM6G): every copy in an account
    shares one records row, and a usage or event listing took the writers' fence and row
    lock, so under load it waited out the 5 s pre-dispatch bound and failed the turn as
    SessionBusyError. Listings now read the last committed records, never the fence."""
    from mux.contracts.ids import PageRequest

    sessions, scope, _, _ = postgres_state
    storage = PostgresGeminiStorage(sessions, scope=scope, project="project")
    driver, _, session = await _gemini_session(storage, scope)
    expected = await driver.usage.list(scope, session.ref, page=PageRequest())
    assert expected.data, "the turn's usage is recorded"

    held, release = asyncio.Event(), asyncio.Event()

    async def writer():  # another copy's turn in the same account, mid-write
        async with storage.transaction():
            held.set()
            await release.wait()

    task = asyncio.create_task(writer())
    await held.wait()
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        pages = await asyncio.wait_for(
            asyncio.gather(
                *(driver.usage.list(scope, session.ref, page=PageRequest()) for _ in range(8)),
                *(driver.events.list(scope, session.ref, page=PageRequest()) for _ in range(8)),
            ),
            timeout=8,
        )
        assert loop.time() - started < 2, "no listing waited behind the writer"
        assert all(page == expected for page in pages[:8]), "the last committed usage"
        assert all(page.data for page in pages[8:])
    finally:
        release.set()
        await task


async def test_a_slow_provider_read_during_reconcile_never_holds_the_records_row(
    postgres_state,
):
    """`_reconcile` used to await each interaction's provider GET inside the account's
    records transaction: one slow read held every other session's writes. The GETs now
    run with no lock held; a short transaction applies their results."""
    sessions, scope, _, _ = postgres_state
    storage = PostgresGeminiStorage(sessions, scope=scope, project="project")
    driver, transport, session = await _gemini_session(storage, scope)
    reading, finish = asyncio.Event(), asyncio.Event()
    real_get = transport.get

    async def slow_get(interaction):
        reading.set()
        await finish.wait()
        return await real_get(interaction)

    transport.get = slow_get
    reconcile = asyncio.create_task(driver.usage.reconcile(scope, session.ref))
    await reading.wait()
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with storage.transaction():  # another session's write, while the GET runs
            pass
        assert loop.time() - started < 1, "the write did not wait for the provider read"
    finally:
        finish.set()
    observations = await reconcile
    assert observations and observations[-1].output_tokens == 20


async def test_a_records_writer_that_gives_up_logs_which_acquisition_failed(
    postgres_state, monkeypatch
):
    """A write that can't get the account's records row in time still fails as busy, and
    the log says which acquisition stopped it (here the advisory fence another writer
    holds), under a fixed label."""
    from daimon.core.turn.errors import SessionBusyError
    from structlog.testing import capture_logs

    monkeypatch.setattr(gemini_runtime_state, "PRE_DISPATCH_BUSY_BOUND_S", 0.3)
    sessions, scope, _, _ = postgres_state
    storage = PostgresGeminiStorage(sessions, scope=scope, project="project")
    async with storage.transaction():
        pass  # the row exists
    held, release = asyncio.Event(), asyncio.Event()

    async def writer():
        async with storage.transaction():
            held.set()
            await release.wait()

    task = asyncio.create_task(writer())
    await held.wait()
    try:
        with capture_logs() as logs, pytest.raises(SessionBusyError):
            async with storage.transaction():
                pass
    finally:
        release.set()
        await task
    named = [e for e in logs if e["event"] == "session_fence.unavailable"]
    assert [e["label"] for e in named] == ["gemini_state:records"]
    assert "advisory" in named[0]["reasons"], named


async def test_a_snapshot_read_that_gets_no_connection_is_busy_and_named(
    postgres_state, monkeypatch
):
    """#1557 review 3: the committed-snapshot read takes no lock, but may wait for a
    pooled connection; that surfaces as the labelled busy error, not a raw pool error."""
    from daimon.core.turn.errors import SessionBusyError
    from sqlalchemy.exc import TimeoutError as PoolTimeoutError
    from structlog.testing import capture_logs

    monkeypatch.setattr(gemini_runtime_state, "PRE_DISPATCH_BUSY_BOUND_S", 0.2)
    sessions, scope, _, _ = postgres_state

    def no_connection():
        raise PoolTimeoutError("QueuePool limit reached")

    database = gemini_runtime_state.PostgresPayloadDatabase(
        no_connection, scope=scope, project="project", kind="records"
    )
    with capture_logs() as logs, pytest.raises(SessionBusyError):
        await database.read()
    named = [e for e in logs if e["event"] == "session_fence.unavailable"]
    assert [(e["label"], e["reasons"]) for e in named] == [
        ("gemini_state:records:read", ["pool_timeout"])
    ]
