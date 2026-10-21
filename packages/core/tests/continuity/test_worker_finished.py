"""Completion writers are closed; future-row fixtures prove reader/lease behavior only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest
from daimon.core._models import (
    Account,
    JournalSession,
    PlatformPrincipal,
    TaskContinuation,
    ThreadSession,
)
from daimon.core._models import FleetRun as FleetRunRow
from daimon.core._models import FleetWorker as FleetWorkerRow
from daimon.core._models import ProviderBinding as BindingRow
from daimon.core.channel_backend import set_channel_backend
from daimon.core.continuity.continuation import (
    ContinuationDecision,
    ContinuationReasonUnavailable,
    ContinuationRequest,
    record_continuation,
)
from daimon.core.continuity.dispatch import WorkerFinishedDecision, dispatch_pending_continuations
from daimon.core.continuity.wakes import (
    WAKE_RETRY_DELAY,
    WAKE_RUN_LEASE,
    WAKE_UNAVAILABLE_DELAY,
    FrozenWorkerBatch,
    WorkerFinishedRootChanged,
    abandon_interrupted_wakes,
    arm_worker_finished,
    claim_wake,
    decide_worker_finished,
    enqueue_wake,
    enqueue_worker_finished,
    list_dispatchable_wakes,
    require_worker_finished_root,
    settle_wake,
    start_wake,
    worker_finished_key,
)
from daimon.core.fleet import FleetRun, FleetWorker
from daimon.core.stores import fleet, mux_state
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.task_continuations import record_continuation as insert_reader_fixture
from daimon.testing.db import build_test_engine, isolated_test_schema, require_owned_sessionmaker
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_fake_anthropic
from mux.contracts.config import BackendConfig
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ChannelRef, ResourceRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.drivers.anthropic.normalize import EventNormalizer
from mux.state.journal import JournalEntry
from mux.state.lease import Slot
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

_NOW = datetime(2026, 10, 12, 18, tzinfo=UTC)


@pytest.fixture
def pooled(db_engine: AsyncEngine, db_clean: None) -> async_sessionmaker[AsyncSession]:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    require_owned_sessionmaker(factory)
    return factory


async def _run(
    sm: async_sessionmaker[AsyncSession],
    *,
    quorum: int | None = None,
    deadline_at: datetime | None = None,
    max_workers: int = 2,
) -> FleetRun:
    async with sm.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
    return await fleet.create_run(
        sm,
        tenant_id=tenant.id,
        orchestrator_account_id=account.id,
        orchestrator_session_id="orchestrator-session",
        orchestrator_binding_id="orchestrator-binding",
        platform="discord",
        channel_id="channel",
        thread_id="thread",
        operation_key="spawn",
        total_budget_usd=Decimal(4),
        deadline_at=deadline_at or _NOW + timedelta(hours=1),
        max_workers=max_workers,
        created_by="authorized-host",
        quorum=quorum,
    )


async def _worker(
    sm: async_sessionmaker[AsyncSession],
    run: FleetRun,
    ordinal: int,
    reviewer_of: uuid.UUID | None = None,
) -> FleetWorker:
    return await fleet.add_worker(
        sm,
        tenant_id=run.tenant_id,
        run_id=run.id,
        ordinal=ordinal,
        role="research",
        spec={},
        spec_digest=f"spec-{ordinal}",
        backend="openai",
        profile="openai.persistent_workspace",
        model="gpt-6-luna",
        budget_usd=Decimal(2),
        deadline_at=run.deadline_at,
        max_turns=1,
        reviewer_of=reviewer_of,
    )


def _request(run: FleetRun) -> ContinuationRequest:
    return ContinuationRequest(
        tenant_id=run.tenant_id,
        requester_account_id=run.orchestrator_account_id,
        requester_external_user_id="human",
        platform="discord",
        parent_channel_id=run.channel_id,
        thread_id=run.thread_id,
        target_ma_agent_id="orchestrator-agent",
        target_name="orchestrator",
        reason="worker_finished",
        requested_work="Collect the completed workers and verify their result contracts.",
        idempotency_key=worker_finished_key(run.id),
    )


async def _seed_reader_fixture(sm: async_sessionmaker[AsyncSession], run: FleetRun) -> None:
    # Deliberate future-row fixture, never a successful host producer proof.
    async with sm.begin() as session:
        await insert_reader_fixture(session, **_request(run).model_dump(), available_at=_NOW)


async def _read(sm: async_sessionmaker[AsyncSession], run: FleetRun) -> TaskContinuationRow | None:
    async with sm() as session:
        return await get_continuation(session, idempotency_key=worker_finished_key(run.id))


@pytest.mark.parametrize("entrypoint", ["worker", "wake", "continuation"])
async def test_completion_producers_refuse_before_any_database_access(entrypoint: str) -> None:
    class ConnectionTrap:
        def in_transaction(self):
            return True

        def begin(self):
            raise AssertionError("Refusal must precede any transaction/connection")

        async def execute(self, *args, **kwargs):
            raise AssertionError("Refusal must precede any query")

    run_id = uuid.uuid4()
    request = ContinuationRequest(
        tenant_id=uuid.uuid4(),
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        requester_account_id=uuid.uuid4(),
        requester_external_user_id="human",
        target_ma_agent_id="agent",
        target_name="orchestrator",
        reason="worker_finished",
        idempotency_key=worker_finished_key(run_id),
        requested_work="Collect the workers.",
    )
    trap = ConnectionTrap()
    if entrypoint == "worker":
        assert not await enqueue_worker_finished(trap, request, run_id=run_id, now=_NOW)
    else:
        with pytest.raises(ContinuationReasonUnavailable, match="compatible-reader release"):
            if entrypoint == "wake":
                await enqueue_wake(trap, request, available_at=_NOW)
            else:
                await record_continuation(trap, request)


@pytest.mark.parametrize("offset", [-1, 0, 1])
@pytest.mark.parametrize("terminal", ["cancelled", "timed_out"])
async def test_readiness_at_deadline_preserves_terminal_transition_without_wake(
    pooled: async_sessionmaker[AsyncSession],
    offset: int,
    terminal: str,
) -> None:
    run = await _run(pooled, deadline_at=datetime.now(UTC) + timedelta(hours=1))
    worker = await _worker(pooled, run, 0)
    if terminal == "timed_out":
        worker = await fleet.claim_worker(pooled, tenant_id=run.tenant_id, run_id=run.id, cap=1)
        assert worker is not None and worker.status == "starting"
    async with pooled.begin() as session:
        await fleet.run_status(session, tenant_id=run.tenant_id, run_id=run.id, for_update=True)
        finished = await fleet.finish(
            session,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=worker.version,
            status=terminal,
            error_code=terminal,
        )
        assert not await enqueue_worker_finished(
            session,
            _request(run),
            run_id=run.id,
            now=run.deadline_at + timedelta(seconds=offset),
        )
    current, workers = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert current == run and current.status == "running" and current.version == run.version
    assert workers == (finished,) and workers[0].status == terminal
    assert workers[0].version == worker.version + 1 and workers[0].error_code == terminal
    async with pooled() as session:
        finished_at = await session.scalar(
            select(FleetWorkerRow.finished_at).where(FleetWorkerRow.id == worker.id)
        )
    assert finished_at is not None
    assert await _read(pooled, run) is None


async def test_inactive_transaction_still_refuses_without_autobegin(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(pooled)
    async with pooled() as session:
        with pytest.raises(ValueError, match="active transaction"):
            await enqueue_worker_finished(session, _request(run), run_id=run.id, now=_NOW)
        assert not session.in_transaction()
        async with session.begin():
            with pytest.raises(ValueError, match="stable wake identity"):
                await enqueue_worker_finished(
                    session,
                    _request(run).model_copy(update={"idempotency_key": uuid.uuid4()}),
                    run_id=run.id,
                    now=_NOW,
                )
    assert await _read(pooled, run) is None


async def _dispatch(
    sm: async_sessionmaker[AsyncSession],
    run: FleetRun,
    decision: WorkerFinishedDecision | None,
    turns: list[uuid.UUID],
    *,
    now: datetime = _NOW,
) -> None:
    def provider(_request):
        raise AssertionError("Worker completion must never fall back to a provider lookup")

    async def legacy_context(_row: TaskContinuationRow):
        raise AssertionError("Worker completion must never use human-history supersession")

    async def notice(_row: TaskContinuationRow, _text: str, _reason: str) -> None:
        raise AssertionError("No notice is requested by these host decisions")

    async def resume(row: TaskContinuationRow, _decision: ContinuationDecision) -> None:
        turns.append(row.idempotency_key)

    async with build_fake_anthropic(provider) as client:
        await dispatch_pending_continuations(
            sm,
            client,
            tenant_id=run.tenant_id,
            platform="discord",
            thread_id=run.thread_id,
            run_decision=resume,
            notice=notice,
            decision_context=legacy_context,
            worker_finished_decision=decision,
            now=lambda: now,
        )


@pytest.mark.parametrize("mode", ["absent", "unready", "busy"])
async def test_unavailable_or_busy_host_decision_defers_without_native_send(
    pooled: async_sessionmaker[AsyncSession], mode: str
) -> None:
    run = await _run(pooled)
    await _seed_reader_fixture(pooled, run)

    async def decision(_row: TaskContinuationRow, _now: datetime) -> ContinuationDecision | None:
        return ContinuationDecision(action="skip_turn_running") if mode == "busy" else None

    turns: list[uuid.UUID] = []
    await _dispatch(pooled, run, None if mode == "absent" else decision, turns)
    row = await _read(pooled, run)
    assert turns == [] and row is not None
    assert row.status == "pending" and row.attempts == 0 and row.started_at is None
    delay = WAKE_UNAVAILABLE_DELAY if mode == "absent" else WAKE_RETRY_DELAY
    assert row.available_at == _NOW + delay


async def test_restart_and_two_dispatchers_resume_at_most_once(
    pooled: async_sessionmaker[AsyncSession],
    db_engine: AsyncEngine,
) -> None:
    run = await _run(pooled)
    await _seed_reader_fixture(pooled, run)
    await db_engine.dispose()
    schema = isolated_test_schema(db_engine)
    assert schema is not None
    engines = [
        build_test_engine(db_engine.url.render_as_string(hide_password=False), schema)
        for _ in range(2)
    ]
    factories = [async_sessionmaker(engine, expire_on_commit=False) for engine in engines]
    for factory in factories:
        require_owned_sessionmaker(factory)

    async def decision(_row: TaskContinuationRow, _now: datetime) -> ContinuationDecision:
        return ContinuationDecision(
            action="dispatch", seed_user_message="Collect the completed workers."
        )

    turns: list[uuid.UUID] = []
    try:
        await asyncio.gather(*(_dispatch(factory, run, decision, turns) for factory in factories))
        await asyncio.gather(*(_dispatch(factory, run, decision, turns) for factory in factories))
        assert turns == [worker_finished_key(run.id)]
        row = await _read(factories[0], run)
        assert row is not None and row.status == "delivered" and row.attempts == 1
    finally:
        for engine in engines:
            await engine.dispose()


async def test_expired_started_worker_wake_never_replays(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(pooled)
    await _seed_reader_fixture(pooled, run)
    claim = await claim_wake(pooled, idempotency_key=worker_finished_key(run.id), now=_NOW)
    assert claim is not None and await start_wake(pooled, claim, now=_NOW)
    expired = _NOW + WAKE_RUN_LEASE + timedelta(seconds=1)
    abandoned = await abandon_interrupted_wakes(pooled, platform="discord", now=expired)
    assert [row.idempotency_key for row in abandoned] == [worker_finished_key(run.id)]
    assert not await settle_wake(pooled, claim, status="delivered", now=expired)
    turns: list[uuid.UUID] = []
    await _dispatch(pooled, run, None, turns, now=expired)
    row = await _read(pooled, run)
    assert row is not None and row.status == "skipped" and row.skip_reason == "interrupted"
    assert turns == []


@pytest.fixture(scope="module")
def prior_queue_reader():
    path = Path(__file__).parent / "fixtures/continuation_prior.txt"
    source = path.read_bytes()
    assert (
        hashlib.sha256(source).hexdigest()
        == "cd530c55e21110be5f223a7b82b142307a5a5046bdd8bb7f48a740a33880c89f"
    )
    module = types.ModuleType("prior_continuation_queue_reader")
    sys.modules[module.__name__] = module
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
        yield module
    finally:
        del sys.modules[module.__name__]


@pytest.mark.parametrize("entrypoint", ["worker", "wake", "continuation"])
async def test_previous_reader_can_dispatch_legacy_timer_after_refused_new_write(
    pooled: async_sessionmaker[AsyncSession],
    prior_queue_reader,
    entrypoint: str,
) -> None:
    run = await _run(pooled)
    request = _request(run)
    timer = request.model_copy(update={"reason": "timer", "idempotency_key": uuid.uuid4()})
    await enqueue_wake(pooled, timer, available_at=_NOW)
    if entrypoint == "worker":
        async with pooled.begin() as session:
            assert not await enqueue_worker_finished(session, request, run_id=run.id, now=_NOW)
    else:
        with pytest.raises(ContinuationReasonUnavailable):
            if entrypoint == "wake":
                await enqueue_wake(pooled, request, available_at=_NOW)
            else:
                await record_continuation(pooled, request)
    assert await _read(pooled, run) is None
    async with pooled.begin() as session:
        pending = await prior_queue_reader.list_pending_continuations(
            session,
            tenant_id=run.tenant_id,
            platform=run.platform,
            thread_id=run.thread_id,
        )
        dispatchable = await prior_queue_reader.list_dispatchable_continuations(
            session,
            tenant_id=run.tenant_id,
            platform=run.platform,
            thread_id=run.thread_id,
            now=_NOW,
            max_attempts=5,
        )
        due = await prior_queue_reader.list_due_wake_rows(
            session,
            platform=run.platform,
            now=_NOW,
            max_attempts=5,
            limit=1,
        )
        for rows in (pending, dispatchable, due):
            assert [row.idempotency_key for row in rows] == [timer.idempotency_key]
            assert rows[0].reason == "timer" and rows[0].attempts == 0
        claimed = await prior_queue_reader.claim_wake_row(
            session,
            idempotency_key=timer.idempotency_key,
            owner="old-process",
            now=_NOW,
            lease=timedelta(minutes=1),
            max_attempts=5,
        )
        assert claimed is not None and claimed.reason == "timer" and claimed.attempts == 1
        assert await prior_queue_reader.start_wake_row(
            session,
            idempotency_key=timer.idempotency_key,
            owner="old-process",
            now=_NOW,
            lease=timedelta(minutes=1),
        )
        assert await prior_queue_reader.settle_wake_row(
            session,
            idempotency_key=timer.idempotency_key,
            owner="old-process",
            status="delivered",
            now=_NOW,
        )
        actual = await get_continuation(session, idempotency_key=timer.idempotency_key)
        assert actual is not None and actual.status == "delivered"
        assert actual.started_at == _NOW and actual.delivered_at == _NOW
    current, workers = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert current == run and workers == ()


async def test_previous_reader_negative_control_still_rejects_injected_future_reason(
    pooled: async_sessionmaker[AsyncSession],
    prior_queue_reader,
) -> None:
    run = await _run(pooled)
    await _seed_reader_fixture(pooled, run)
    timer = _request(run).model_copy(update={"reason": "timer", "idempotency_key": uuid.uuid4()})
    await enqueue_wake(pooled, timer, available_at=_NOW)
    async with pooled() as session:
        with pytest.raises(ValidationError, match="reason"):
            await prior_queue_reader.list_dispatchable_continuations(
                session,
                tenant_id=run.tenant_id,
                platform=run.platform,
                thread_id=run.thread_id,
                now=_NOW,
                max_attempts=5,
            )


async def _arm(session, run, workers, *, now=_NOW):
    ids = tuple(w.id for w in workers)
    return await arm_worker_finished(
        session,
        _request(run).model_copy(update={"idempotency_key": worker_finished_key(run.id, ids)}),
        run_id=run.id,
        worker_ids=ids,
        now=now,
    )


async def _finish_batch(session, run, worker, *, now=_NOW):
    await fleet.run_status(session, tenant_id=run.tenant_id, run_id=run.id, for_update=True)
    await fleet.finish(
        session,
        tenant_id=run.tenant_id,
        worker_id=worker.id,
        expected_version=worker.version,
        status="cancelled",
        error_code="stopped",
    )
    return await enqueue_worker_finished(session, tenant_id=run.tenant_id, run_id=run.id, now=now)


@pytest.mark.parametrize("quorum", [None, 1])
async def test_frozen_initial_and_review_waits_do_not_count_each_others_workers(pooled, quorum):
    run = await _run(pooled, quorum=quorum, max_workers=4)
    first, late = [await _worker(pooled, run, i) for i in range(2)]
    async with pooled.begin() as db:
        initial = await _arm(db, run, [first, late])
        assert initial is not None
        assert await _arm(db, run, [late, first]) == initial
        changed = _request(run).model_copy(
            update={"idempotency_key": initial.idempotency_key, "target_name": "other"}
        )
        with pytest.raises(ValueError, match="different frozen intent"):
            await arm_worker_finished(
                db, changed, run_id=run.id, worker_ids=[first.id, late.id], now=_NOW
            )
    assert not await list_dispatchable_wakes(
        pooled, tenant_id=run.tenant_id, platform=run.platform, thread_id=run.thread_id, now=_NOW
    )
    async with pooled.begin() as db:
        assert await _finish_batch(db, run, first) is (quorum == 1)
    review = await _worker(pooled, run, 2, reviewer_of=first.id)
    other_review = await _worker(pooled, run, 3, reviewer_of=late.id)
    async with pooled.begin() as db:
        with pytest.raises(ValueError, match="separate batches"):
            await _arm(db, run, [first, review])
        second = await _arm(db, run, [review, other_review])
        assert second is not None and second.idempotency_key != initial.idempotency_key
        batch = FrozenWorkerBatch.model_validate_json(second.requested_work)
        assert set(batch.worker_ids) == {review.id, other_review.id} and batch.quorum == 2
        assert batch.binding_id == run.orchestrator_binding_id
        assert batch.session_id == run.orchestrator_session_id
        assert await _finish_batch(db, run, late) is (quorum is None)
        assert not await enqueue_worker_finished(
            db, tenant_id=run.tenant_id, run_id=run.id, now=_NOW
        )
    async with pooled.begin() as db:
        assert not await _finish_batch(db, run, review)
        assert await _finish_batch(db, run, other_review)
        assert not await enqueue_worker_finished(
            db, tenant_id=run.tenant_id, run_id=run.id, now=_NOW
        )
    rows = await list_dispatchable_wakes(
        pooled, tenant_id=run.tenant_id, platform=run.platform, thread_id=run.thread_id, now=_NOW
    )
    assert {r.idempotency_key for r in rows} == {initial.idempotency_key, second.idempotency_key}
    current, _ = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert current == run and current.status == "running"


@pytest.mark.parametrize("offset", [-1, 0, 1])
async def test_armed_wait_deadline_never_rolls_back_terminal_move(pooled, offset):
    run = await _run(pooled)
    worker = await _worker(pooled, run, 0)
    async with pooled.begin() as db:
        row = await _arm(db, run, [worker])
    async with pooled.begin() as db:
        assert await _finish_batch(
            db, run, worker, now=run.deadline_at + timedelta(seconds=offset)
        ) is (offset < 0)
    _, workers = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert workers[0].status == "cancelled" and workers[0].version == worker.version + 1
    async with pooled() as db:
        actual = await get_continuation(db, idempotency_key=row.idempotency_key)
    assert (actual.available_at == row.available_at) is (offset >= 0)


async def test_arm_and_completion_roll_back_and_completed_before_arm_is_not_lost(pooled):
    run = await _run(pooled)
    worker = await _worker(pooled, run, 0)
    async with pooled() as db, db.begin():
        assert not await _finish_batch(db, run, worker)
        row = await _arm(db, run, [worker])
        assert row.available_at == _NOW
        await db.rollback()
    async with pooled() as db:
        assert await get_continuation(db, idempotency_key=row.idempotency_key) is None
    async with pooled.begin() as db:
        assert not await _finish_batch(db, run, worker)
    async with pooled.begin() as db:
        row = await _arm(db, run, [worker])
        assert row.available_at == _NOW


@pytest.mark.parametrize("mutation", ["account", "channel", "key", "duplicate", "foreign"])
async def test_freezing_refuses_changed_address_identity_or_members(pooled, mutation):
    run = await _run(pooled)
    worker = await _worker(pooled, run, 0)
    ids = [worker.id]
    request = _request(run).model_copy(update={"idempotency_key": worker_finished_key(run.id, ids)})
    if mutation == "account":
        request = request.model_copy(update={"requester_account_id": uuid.uuid4()})
    elif mutation == "channel":
        request = request.model_copy(update={"parent_channel_id": "other"})
    elif mutation == "key":
        request = request.model_copy(update={"idempotency_key": uuid.uuid4()})
    elif mutation == "duplicate":
        ids.append(worker.id)
    else:
        ids = [uuid.uuid4()]
        request = request.model_copy(update={"idempotency_key": worker_finished_key(run.id, ids)})
    async with pooled.begin() as db:
        with pytest.raises(ValueError):
            await arm_worker_finished(db, request, run_id=run.id, worker_ids=ids, now=_NOW)
    assert await _read(pooled, run) is None


@pytest.mark.parametrize("quorum", [None, 1])
async def test_racing_terminal_moves_publish_once_and_restart_keeps_the_fence(
    pooled, db_engine, quorum
):
    run = await _run(pooled, quorum=quorum)
    workers = [await _worker(pooled, run, i) for i in range(2)]
    async with pooled.begin() as db:
        row = await _arm(db, run, workers)

    async def complete(worker):
        async with pooled.begin() as db:
            return await _finish_batch(db, run, worker)

    assert sum(await asyncio.gather(*(complete(w) for w in workers))) == 1
    claim = await claim_wake(pooled, idempotency_key=row.idempotency_key, now=_NOW)
    takeover_at = _NOW + timedelta(minutes=6)
    schema = isolated_test_schema(db_engine)
    assert schema is not None
    engine = build_test_engine(db_engine.url.render_as_string(hide_password=False), schema)
    restarted = async_sessionmaker(engine, expire_on_commit=False)
    require_owned_sessionmaker(restarted)
    try:
        reclaimed = await claim_wake(
            restarted, idempotency_key=row.idempotency_key, now=takeover_at
        )
        assert reclaimed is not None and reclaimed.owner != claim.owner
        assert not await start_wake(pooled, claim, now=takeover_at)
        assert await start_wake(restarted, reclaimed, now=takeover_at)
        assert not await claim_wake(
            pooled,
            idempotency_key=row.idempotency_key,
            now=takeover_at + WAKE_RUN_LEASE + timedelta(seconds=1),
        )
    finally:
        await engine.dispose()


def _root_event(ref, kind, payload):
    event = Event(
        id=str(uuid.uuid4()),
        session_id=ref.id,
        sequence=0,
        type=kind,
        observed_at=_NOW,
        authority="record",
        payload=payload,
        native=NativeProvenance(provider=ref.provider, api_revision="fixture"),
    )
    return JournalEntry(source_key=event.id, event=event)


async def _ready_root(pooled, provider="anthropic", *, binding_id=None):
    run = await _run(pooled, quorum=1)
    workers = [await _worker(pooled, run, i) for i in range(2)]
    identity = dict(
        tenant_id=run.tenant_id, platform=run.platform, account_id=run.orchestrator_account_id
    )
    async with pooled.begin() as db:
        db.add(PlatformPrincipal(**identity, external_id="human"))
        db.add(
            ThreadSession(
                **identity,
                thread_id=run.thread_id,
                ma_session_id=run.orchestrator_session_id,
                ma_agent_id="orchestrator-agent",
                channel_id=run.channel_id,
            )
        )
        row = await _arm(db, run, workers)
        assert await _finish_batch(db, run, workers[0])
    slot = Slot(
        thread=ThreadRef(
            channel=ChannelRef(
                tenant_id=str(run.tenant_id), platform=run.platform, channel_id=run.channel_id
            ),
            thread_id=run.thread_id,
        ),
        account_id=str(run.orchestrator_account_id),
    )
    # Guard resolution validates the immutable revision, just as admission does.
    async with pooled.begin() as db:
        revision = await set_channel_backend(
            db,
            slot.thread.channel,
            BackendConfig(
                backend=provider,
                profile={
                    "anthropic": "anthropic.managed_agents",
                    "openai": "openai.persistent_workspace",
                    "gemini": "gemini.inline_reuse",
                }[provider],
                model="fixture-model",
            ),
        )
    binding = ProviderBinding(
        id=binding_id or run.orchestrator_binding_id,
        thread=slot.thread,
        provider=provider,
        profile=revision.profile,
        native_refs={"session": run.orchestrator_session_id, "agent": f"{provider}-native-agent"},
        generation=1,
        config_revision=revision.local,
        legacy_account_id=slot.account_id,
    )
    ref = ResourceRef(
        id=run.orchestrator_session_id,
        kind="session",
        provider=provider,
        account_scope_id="workspace",
    )
    store = mux_state.PostgresStateStore(pooled, trust_caller_clock=True)
    await store.put_binding(binding, expected_generation=0)
    lease = await store.acquire_lease(
        slot, holder="root", turn_id="root", now=_NOW, ttl=timedelta(minutes=5)
    )
    entries = [
        _root_event(ref, "session.status_running", {"root_turn_id": "root"}),
        _root_event(ref, "session.turn_ended", {"root_turn_id": "root", "outcome": "completed"}),
    ]
    await store.append_events(ref, entries, fence=lease, cursor="root-ended", now=_NOW)
    await store.release_lease(lease)
    return run, row, store, slot, ref


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_durable_decision_seeds_the_original_root_and_dispatches_once(pooled, provider):
    run, row, _, _, _ = await _ready_root(pooled, provider)
    decision = await decide_worker_finished(pooled, row, _NOW)
    assert decision.action == "dispatch" and len(decision.seed_user_message) <= 500
    assert str(run.id) in decision.seed_user_message
    assert str(row.idempotency_key) in decision.seed_user_message
    assert "cancelled" in decision.seed_user_message and "pending" in decision.seed_user_message

    turns = []
    for _ in range(2):
        await _dispatch(pooled, run, partial(decide_worker_finished, pooled), turns)
    assert turns == [row.idempotency_key]
    async with pooled() as db:
        settled = await get_continuation(db, idempotency_key=row.idempotency_key)
    assert settled.status == "delivered" and settled.attempts == 1


@pytest.mark.parametrize(
    ("table", "field", "path", "value"),
    [
        (BindingRow, "binding", ("native_refs", "session"), "other"),
        (BindingRow, "binding", ("legacy_account_id",), "other"),
        (JournalSession, "projection", (), None),
        (JournalSession, "projection", ("state",), "terminated"),
        (JournalSession, "projection", ("gaps",), ["missing"]),
        (JournalSession, "projection", ("session", "account_id"), "other"),
        (JournalSession, "projection", ("session", "provider"), "gemini"),
        (PlatformPrincipal, "external_id", (), "other"),
        (Account, "tenant_id", (), "foreign_tenant"),
        (FleetWorkerRow, "status", (), "running"),
        (FleetRunRow, "status", (), "cancelled"),
        (TaskContinuation, "thread_id", (), "other"),
        (TaskContinuation, "requested_work", ("binding_id",), "other"),
        (TaskContinuation, "requested_work", ("session_id",), "other"),
        (TaskContinuation, "requested_work", ("worker_ids",), [str(uuid.UUID(int=0))]),
        (TaskContinuation, "requested_work", ("quorum",), 2),
        (TaskContinuation, "requested_work", ("run_id",), str(uuid.UUID(int=0))),
        (TaskContinuation, "status", (), "delivered"),
        (TaskContinuation, "available_at", (), _NOW + timedelta(minutes=5)),
        (ThreadSession, "ma_session_id", (), "other"),
        (ThreadSession, "ma_agent_id", (), "other"),
        (ThreadSession, "channel_id", (), "other"),
    ],
)
@pytest.mark.parametrize("sending", [False, True])
async def test_missing_stale_or_incomplete_root_facts_never_authorize_a_turn(
    pooled, table, field, path, value, sending
):
    run, row, _, _, _ = await _ready_root(pooled)
    if sending:
        row = await _start_root_wait(pooled, row)
    async with pooled.begin() as db:
        record = (await db.scalars(select(table).where(table.tenant_id == run.tenant_id))).first()
        changed = getattr(record, field) if path else value
        if path and field == "requested_work":
            changed = json.loads(changed)
        if path:
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
        if field == "requested_work":
            changed = json.dumps(changed, separators=(",", ":"))
        if value == "foreign_tenant":
            changed = (await make_tenant(db)).id
        await db.execute(
            update(table).where(table.tenant_id == run.tenant_id).values({field: changed})
        )
        if field == "requested_work":
            row = await get_continuation(db, idempotency_key=row.idempotency_key)
    if sending:
        with pytest.raises(WorkerFinishedRootChanged):
            await _completion_fence(pooled, run, row)
    else:
        assert await decide_worker_finished(pooled, row, _NOW) is None


@pytest.mark.parametrize("stale", ["binding", "deadline"])
async def test_an_original_root_rebound_or_expired_never_dispatches(pooled, stale):
    run, row, _, _, _ = await _ready_root(
        pooled, binding_id="rebound" if stale == "binding" else None
    )
    now = run.deadline_at if stale == "deadline" else _NOW
    assert await decide_worker_finished(pooled, row, now) is None


@pytest.mark.parametrize("busy", ["lease", "journal", "legacy"])
async def test_live_root_turn_defers_and_keeps_the_same_wait(pooled, busy):
    run, row, store, slot, ref = await _ready_root(pooled)
    if busy == "legacy":
        async with pooled.begin() as db:
            await db.execute(
                update(ThreadSession)
                .where(ThreadSession.tenant_id == run.tenant_id)
                .values(active_turn_message_id="next")
            )
    else:
        lease = await store.acquire_lease(
            slot, holder="next", turn_id="next", now=_NOW, ttl=timedelta(minutes=5)
        )
    if busy == "journal":
        await store.append_events(
            ref,
            [_root_event(ref, "session.status_running", {"root_turn_id": "next"})],
            fence=lease,
            cursor="next-running",
            now=_NOW,
        )
        await store.release_lease(lease)
    with capture_logs() as logs:
        assert (await decide_worker_finished(pooled, row, _NOW)).action == "skip_turn_running"
    deferred = [entry for entry in logs if entry["event"] == "fleet.worker_finished_deferred"]
    assert [entry["action"] for entry in deferred] == ["skip_turn_running"]
    expected = "live_turn_marker" if busy == "legacy" else "projection_busy"
    assert deferred[0]["reason"].startswith(expected)
    assert deferred[0]["continuation_id"] == str(row.id)


async def test_a_deferred_wake_names_why_it_did_not_dispatch(pooled):
    run, row, _, _, _ = await _ready_root(pooled)
    with capture_logs() as logs:
        assert await decide_worker_finished(pooled, row, run.deadline_at) is None
    assert [(entry["action"], entry["reason"]) for entry in logs] == [(None, "run_not_running")]


async def test_root_turn_recorded_running_before_its_input_still_wakes(pooled):
    # Staging r11 (2026-10-18): Managed Agents recorded status_running before the
    # user.message that opened the root turn. The journal must fold the end_turn so
    # the projection returns to idle and the finished workers wake the root.
    run, row, store, slot, ref = await _ready_root(pooled)
    lease = await store.acquire_lease(
        slot, holder="root", turn_id="root", now=_NOW, ttl=timedelta(minutes=5)
    )
    normalizer = EventNormalizer(ref)
    records = [
        {"type": "session.status_running", "id": "sevt_running"},
        {"type": "user.message", "id": "sevt_input", "content": [{"type": "text", "text": "go"}]},
        {"type": "session.status_idle", "id": "sevt_idle", "stop_reason": {"type": "end_turn"}},
    ]
    entries = [
        JournalEntry(
            source_key=record["id"],
            event=normalizer.normalize(
                {**record, "processed_at": _NOW.isoformat()}, observed_at=_NOW
            ),
        )
        for record in records
    ]
    await store.append_events(ref, entries, fence=lease, cursor="sevt_idle", now=_NOW)
    await store.release_lease(lease)
    async with pooled() as db:
        projection = await mux_state.projection(db, ref.id)
    assert projection.state == "idle" and projection.active_root_turn is None
    assert (await decide_worker_finished(pooled, row, _NOW)).action == "dispatch"


async def _start_root_wait(pooled, row):
    claim = await claim_wake(pooled, idempotency_key=row.idempotency_key, now=_NOW)
    assert claim is not None
    assert await start_wake(pooled, claim, now=_NOW)
    async with pooled() as db:
        return await get_continuation(db, idempotency_key=row.idempotency_key)


async def _completion_fence(pooled, run, row, **changes):
    arguments = dict(
        session_id=run.orchestrator_session_id,
        account_id=run.orchestrator_account_id,
        agent_id=row.target_ma_agent_id,
        now=_NOW,
    )
    arguments.update(changes)
    await require_worker_finished_root(pooled, row, **arguments)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_started_completion_fence_accepts_its_original_root_and_own_active_turn(
    pooled, provider
):
    run, row, store, slot, ref = await _ready_root(pooled, provider)
    row = await _start_root_wait(pooled, row)
    lease = await store.acquire_lease(
        slot, holder="completion", turn_id="completion", now=_NOW, ttl=timedelta(minutes=5)
    )
    await store.append_events(
        ref,
        [_root_event(ref, "session.status_running", {"root_turn_id": "completion"})],
        fence=lease,
        cursor="completion",
        now=_NOW,
    )
    async with pooled.begin() as db:
        await db.execute(
            update(ThreadSession)
            .where(ThreadSession.tenant_id == run.tenant_id)
            .values(active_turn_message_id="completion")
        )
    await _completion_fence(pooled, run, row)
    assert await decide_worker_finished(pooled, row, _NOW) is None


@pytest.mark.parametrize("claim_only", [False, True])
async def test_completion_fence_refuses_without_the_real_start_fence(pooled, claim_only):
    run, row, _, _, _ = await _ready_root(pooled)
    if claim_only:
        assert await claim_wake(pooled, idempotency_key=row.idempotency_key, now=_NOW)
    with pytest.raises(WorkerFinishedRootChanged):
        await _completion_fence(pooled, run, row)


@pytest.mark.parametrize("field", ["session_id", "account_id", "agent_id"])
async def test_completion_fence_rejects_changed_preparation(pooled, field):
    run, row, _, _, _ = await _ready_root(pooled)
    row = await _start_root_wait(pooled, row)
    value = uuid.uuid4() if field == "account_id" else "changed"
    with pytest.raises(WorkerFinishedRootChanged):
        await _completion_fence(pooled, run, row, **{field: value})


@pytest.mark.parametrize("changed", ["owner", "attempt", "started", "expired"])
async def test_completion_fence_requires_the_exact_live_started_claim(pooled, changed):
    run, row, _, _, _ = await _ready_root(pooled)
    row = await _start_root_wait(pooled, row)
    if changed == "owner":
        row = row.model_copy(update={"lease_owner": "another-dispatcher"})
    elif changed == "attempt":
        row = row.model_copy(update={"attempts": row.attempts + 1})
    elif changed == "started":
        row = row.model_copy(update={"started_at": row.started_at + timedelta(seconds=1)})
    with pytest.raises(WorkerFinishedRootChanged):
        await _completion_fence(
            pooled, run, row, **({"now": row.lease_expires_at} if changed == "expired" else {})
        )


async def test_worker_dispatch_callback_receives_the_persisted_started_claim(pooled):
    run, queued, _, _, _ = await _ready_root(pooled)
    receipts = []

    async def run_decision(row, decision):
        async with pooled() as db:
            actual = await get_continuation(db, idempotency_key=row.idempotency_key)
        assert row == actual and row.started_at == _NOW
        assert row.lease_owner and row.attempts == 1 and row.status == "claimed"
        await _completion_fence(pooled, run, row)
        receipts.append(row)

    async def context(row):
        return None, False

    def no_native(request):
        raise AssertionError(
            "A claimed completion callback must not consult native legacy admission"
        )

    async def notice(row, text, reason):
        raise AssertionError("The valid completion must dispatch without a notice")

    async with build_fake_anthropic(no_native) as client:
        await dispatch_pending_continuations(
            pooled,
            client,
            tenant_id=run.tenant_id,
            platform="discord",
            thread_id=run.thread_id,
            now=lambda: _NOW,
            worker_finished_decision=partial(decide_worker_finished, pooled),
            run_decision=run_decision,
            decision_context=context,
            notice=notice,
        )
    assert len(receipts) == 1 and receipts[0].id == queued.id
