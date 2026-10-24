"""Fleet store: the run/worker rows, the tenant worker pool, results in the journal."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any

import pytest
from daimon.core.fleet import (
    FleetConflict,
    FleetRun,
    FleetWorker,
    ResultPointer,
    append_result,
    result_digest,
    run_settled,
    spawn_operation_key,
    worker_slot,
)
from daimon.core.stores import fleet, mux_state
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.fleet import WorkerPool
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn_queue import TurnQueue
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.resources import ProviderBinding
from mux.state.journal import JournalEntry
from mux.state.memory import MemoryStateStore
from mux.state.operations import SendClaimed, recovery
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
# Deadlines are against the database clock, so they sit well after the real now.
LATER = datetime.now(UTC) + timedelta(days=1)
SPEC: dict[str, Any] = {"backend": "openai", "model": "gpt-6-luna", "system": "Research."}


@pytest.fixture
async def pooled(db_engine: AsyncEngine, db_clean: None) -> async_sessionmaker[AsyncSession]:
    """Pooled connections of their own, so concurrent claims really race."""
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def _run(
    sm: async_sessionmaker[AsyncSession],
    *,
    max_workers: int = 4,
    budget: str = "10",
    key: str = "op-1",
    quorum: int | None = None,
    tenant: TenantRow | None = None,
) -> FleetRun:
    async with sm.begin() as session:
        tenant = tenant or await make_tenant(session)
        account = await make_account(session, tenant=tenant)
    return await fleet.create_run(
        sm,
        tenant_id=tenant.id,
        orchestrator_account_id=account.id,
        orchestrator_session_id="sess_orchestrator",
        orchestrator_binding_id="bind_orchestrator",
        platform="discord",
        channel_id="900000000000000001",
        thread_id="900000000000000002",
        operation_key=key,
        total_budget_usd=Decimal(budget),
        deadline_at=LATER,
        max_workers=max_workers,
        created_by="mcp:orchestrator",
        quorum=quorum,
    )


async def _worker(
    sm: async_sessionmaker[AsyncSession],
    run: FleetRun,
    ordinal: int,
    *,
    digest: str = "d1",
    reviewer_of: uuid.UUID | None = None,
) -> FleetWorker:
    return await fleet.add_worker(
        sm,
        tenant_id=run.tenant_id,
        run_id=run.id,
        ordinal=ordinal,
        role="research",
        spec=SPEC,
        spec_digest=digest,
        backend="openai",
        profile="openai.persistent_workspace",
        model="gpt-6-luna",
        budget_usd=Decimal("2"),
        deadline_at=LATER,
        max_turns=3,
        reviewer_of=reviewer_of,
    )


# Pure rules


def _status_worker(status: str, **extra: Any) -> FleetWorker:
    base = dict(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        ordinal=0,
        role="r",
        spec={},
        spec_digest="d",
        backend="openai",
        profile="p",
        model="m",
        binding_id=None,
        status=status,
        budget_usd=Decimal(1),
        reserved_usd=Decimal(0),
        deadline_at=LATER,
        max_turns=1,
        turns_used=0,
        attempts=0,
        reviewer_of=None,
        result_contract=None,
        result=None,
        error_code=None,
        version=1,
    )
    return FleetWorker(**{**base, **extra})  # type: ignore[arg-type]


def _plain_run(**overrides: Any) -> FleetRun:
    base: dict[str, Any] = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        orchestrator_account_id=uuid.uuid4(),
        orchestrator_session_id="s",
        orchestrator_binding_id="b",
        platform="discord",
        channel_id="c",
        thread_id="t",
        status="running",
        total_budget_usd=Decimal(1),
        reserved_usd=Decimal(0),
        observed_usd=Decimal(0),
        deadline_at=LATER,
        max_workers=1,
        quorum=None,
        version=1,
    )
    return FleetRun(**{**base, **overrides})


def test_a_run_settles_when_every_worker_ends_or_its_quorum_does() -> None:
    run = _plain_run(max_workers=3)
    done, failed, live = (
        _status_worker("succeeded"),
        _status_worker("failed"),
        _status_worker("running"),
    )
    assert not run_settled(run, [])
    assert not run_settled(run, [done, live])
    assert run_settled(run, [done, failed])
    quorum = _plain_run(max_workers=3, quorum=1)
    assert run_settled(quorum, [done, live])
    assert not run_settled(quorum, [live])


def test_a_worker_slot_is_private_to_the_orchestrator_and_one_per_ordinal() -> None:
    run = _plain_run(max_workers=2)
    first, second = _status_worker("pending"), _status_worker("pending", ordinal=1)
    slot = worker_slot(run, first)
    assert slot.account_id == str(run.orchestrator_account_id)  # never the shared slot
    assert slot.thread.thread_id == f"fleet/{run.id}/0"
    assert slot.thread.channel.tenant_id == str(run.tenant_id)
    assert worker_slot(run, second) != slot
    assert spawn_operation_key(run.id, 1) == f"fleet:{run.id}:spawn:1"


# Results in the worker's own journal


def _final_message(session_id: str, text: str) -> Event:
    return Event.model_validate(
        {
            "id": "ev_final",
            "session_id": session_id,
            "sequence": 0,
            "type": "agent.message",
            "turn_id": "turn_1",
            "observed_at": NOW,
            "authority": "record",
            "payload": {"item_id": "m1", "content": [{"type": "text", "text": text}]},
            "native": NativeProvenance(provider="openai", api_revision="v1"),
        }
    )


async def _bind(
    store: Any, run: FleetRun, worker: FleetWorker, *, session_id: str = "sess_w0"
) -> tuple[Any, ResourceRef, Any, str]:
    """Bind a worker's private slot to its session and take the slot's lease."""
    slot = worker_slot(run, worker)
    session = ResourceRef(id=session_id, kind="session", provider="openai", account_scope_id="p")
    binding_id = f"bind_{uuid.uuid4()}"
    await store.put_binding(
        ProviderBinding(
            id=binding_id,
            thread=slot.thread,
            provider="openai",
            profile="openai.persistent_workspace",
            native_refs={"session": session.id},
            generation=1,
            config_revision=1,
            legacy_account_id=slot.account_id,
        ),
        expected_generation=0,
    )
    lease = await store.acquire_lease(
        slot, holder="p1", turn_id="turn_1", now=NOW, ttl=timedelta(minutes=5)
    )
    return slot, session, lease, binding_id


@pytest.fixture(params=["memory", "postgres"])
async def bound(
    request: pytest.FixtureRequest, db_session_factory: async_sessionmaker[AsyncSession]
) -> tuple[Any, ...]:
    """A worker's bound session and lease, on the memory store or Postgres."""
    if request.param == "memory":
        store: Any = MemoryStateStore()
        run = _plain_run(max_workers=2)
    else:
        store = PostgresStateStore(db_session_factory, trust_caller_clock=True)
        run = await _run(db_session_factory)
    worker = _status_worker("running", run_id=run.id, tenant_id=run.tenant_id)
    slot, session, lease, _ = await _bind(store, run, worker)
    return store, run, worker, slot, session, lease


async def _successor(store: Any, slot: Any) -> Any:
    later = NOW + timedelta(minutes=6)
    lease = await store.acquire_lease(
        slot, holder="p2", turn_id="turn_1", now=later, ttl=timedelta(minutes=5)
    )
    assert lease.took_over
    return lease


async def test_a_result_after_the_turn_journaled_its_final_message_is_one_entry_of_its_own(
    bound: tuple[Any, ...],
) -> None:
    store, run, worker, slot, session, lease = bound
    final = _final_message(session.id, "the answer")
    # The worker turn already journaled its final message under its native key.
    await store.append_events(
        session,
        [JournalEntry(source_key="native:m1", event=final)],
        fence=lease,
        cursor="c0",
        now=NOW,
    )

    first = await append_result(
        store, run, worker, session, final, lease=lease, cursor="c1", now=NOW
    )
    successor = await _successor(store, slot)
    again = await append_result(
        store,
        run,
        worker,
        session,
        final,
        lease=successor,
        cursor="c1",
        now=NOW + timedelta(minutes=6),
    )

    assert again == first
    ids = [event.id for event in await store.read_events(session.id)]
    assert ids == ["ev_final", f"fleet:{worker.id}:result"]  # no event id twice
    assert first.sequence == 1 and first.excerpt == "the answer"
    assert first.digest == result_digest(dict(final.payload))


async def test_a_successor_with_a_rebuilt_event_gets_the_stored_result(
    bound: tuple[Any, ...],
) -> None:
    store, run, worker, slot, session, lease = bound
    first = await append_result(
        store,
        run,
        worker,
        session,
        _final_message(session.id, "the answer"),
        lease=lease,
        cursor="c1",
        now=NOW,
    )
    successor = await _successor(store, slot)
    rebuilt = _final_message(session.id, "the answer, reconstructed").model_copy(
        update={"id": "ev_rebuilt"}
    )

    again = await append_result(
        store,
        run,
        worker,
        session,
        rebuilt,
        lease=successor,
        cursor="c1",
        now=NOW + timedelta(minutes=6),
    )

    assert again == first  # the first stored result stands
    assert [event.id for event in await store.read_events(session.id)] == [
        f"fleet:{worker.id}:result"
    ]


# The store


async def test_a_worker_is_bounded_by_its_run(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)

    worker = await _worker(db_session_factory, run, 0)
    assert await _worker(db_session_factory, run, 0) == worker
    with pytest.raises(FleetConflict, match="exists with other spec_digest"):
        await _worker(db_session_factory, run, 0, digest="d2")
    with pytest.raises(FleetConflict, match="allows 4 workers"):
        await _worker(db_session_factory, run, 8)
    with pytest.raises(FleetConflict, match="outlast"):
        await fleet.add_worker(
            db_session_factory,
            tenant_id=run.tenant_id,
            run_id=run.id,
            ordinal=1,
            role="r",
            spec=SPEC,
            spec_digest="d",
            backend="openai",
            profile="p",
            model="m",
            budget_usd=Decimal(1),
            deadline_at=LATER + timedelta(seconds=1),
            max_turns=1,
        )
    for ordinal in (1, 2, 3):
        await _worker(db_session_factory, run, ordinal)
    # Four workers hold all four slots; only a spawn refused at admission frees one.
    with pytest.raises(FleetConflict, match="allows 4 workers"):
        await _worker(db_session_factory, run, 4)


async def test_a_reviewer_must_be_another_worker_of_the_same_run(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
    run = await _run(db_session_factory, tenant=tenant)
    other = await _run(db_session_factory, key="op-2", tenant=tenant)
    reviewed = await _worker(db_session_factory, run, 0)
    reviewer = await _worker(db_session_factory, run, 1, reviewer_of=reviewed.id)
    assert reviewer.reviewer_of == reviewed.id
    elsewhere = await _worker(db_session_factory, other, 0)
    with pytest.raises(IntegrityError):  # a worker of another run cannot be reviewed
        await _worker(db_session_factory, run, 2, reviewer_of=elsewhere.id)


async def test_a_worker_moves_only_along_its_transitions_under_a_version_cas(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    worker = await _worker(db_session_factory, run, 0)
    sm, tenant = db_session_factory, run.tenant_id

    with pytest.raises(FleetConflict):  # pending cannot run before it is claimed
        await fleet.mark_running(
            sm, tenant_id=tenant, worker_id=worker.id, expected_version=1, binding_id="b"
        )
    claimed = await fleet.claim_worker(sm, tenant_id=tenant, run_id=run.id, cap=3)
    assert claimed is not None and claimed.status == "starting" and claimed.version == 2
    with pytest.raises(FleetConflict):  # a stale version loses
        await fleet.mark_running(
            sm, tenant_id=tenant, worker_id=worker.id, expected_version=1, binding_id="b"
        )
    store = PostgresStateStore(sm, trust_caller_clock=True)
    _, session, lease, binding_id = await _bind(store, run, worker)
    running = await fleet.mark_running(
        sm, tenant_id=tenant, worker_id=worker.id, expected_version=2, binding_id=binding_id
    )
    assert running.binding_id == binding_id
    pointer = await append_result(
        store,
        run,
        running,
        session,
        _final_message(session.id, "ok"),
        lease=lease,
        cursor="c",
        now=NOW,
    )
    done = await fleet.record_result(
        sm, tenant_id=tenant, worker_id=worker.id, expected_version=3, result=pointer
    )
    assert done.status == "succeeded" and done.result == pointer
    for status in ("failed", "cancelled", "pending"):
        with pytest.raises(FleetConflict):  # a terminal worker never moves
            await fleet.finish(
                sm,
                tenant_id=tenant,
                worker_id=worker.id,
                expected_version=4,
                status=status,  # type: ignore[arg-type]
                error_code="x",
            )


async def test_two_claims_cannot_both_win_one_worker(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    await _worker(db_session_factory, run, 0)
    first = await fleet.claim_worker(
        db_session_factory, tenant_id=run.tenant_id, run_id=run.id, cap=3
    )
    second = await fleet.claim_worker(
        db_session_factory, tenant_id=run.tenant_id, run_id=run.id, cap=3
    )
    assert first is not None and second is None


async def test_the_tenant_pool_never_exceeds_its_cap_under_concurrent_claims(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(pooled, max_workers=8)
    for ordinal in range(8):
        await _worker(pooled, run, ordinal)
    claims = await asyncio.gather(
        *(
            fleet.claim_worker(pooled, tenant_id=run.tenant_id, run_id=run.id, cap=3)
            for _ in range(8)
        )
    )
    won = [claim for claim in claims if claim is not None]
    assert len(won) == 3
    assert len({claim.id for claim in won}) == 3
    async with pooled() as session:
        active = (
            await session.execute(
                text(
                    "SELECT count(*) FROM fleet_worker "
                    "WHERE tenant_id = :t AND status IN ('starting', 'running')"
                ),
                {"t": run.tenant_id},
            )
        ).scalar_one()
    assert active == 3

    # Ending one frees exactly one place.
    await fleet.finish(
        pooled,
        tenant_id=run.tenant_id,
        worker_id=won[0].id,
        expected_version=won[0].version,
        status="cancelled",
        error_code="stopped",
    )
    assert await fleet.claim_worker(pooled, tenant_id=run.tenant_id, run_id=run.id, cap=3)
    assert await fleet.claim_worker(pooled, tenant_id=run.tenant_id, run_id=run.id, cap=3) is None


async def test_the_cap_is_per_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mine, theirs = await _run(db_session_factory), await _run(db_session_factory, key="op-x")
    await _worker(db_session_factory, mine, 0)
    await _worker(db_session_factory, theirs, 0)
    assert await fleet.claim_worker(
        db_session_factory, tenant_id=mine.tenant_id, run_id=mine.id, cap=1
    )
    assert await fleet.claim_worker(
        db_session_factory, tenant_id=theirs.tenant_id, run_id=theirs.id, cap=1
    )


async def test_the_process_pool_gives_its_slot_back_when_the_tenant_has_no_room(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    await _worker(db_session_factory, run, 0)
    await _worker(db_session_factory, run, 1)
    pool = WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=5)

    claimed = await pool.claim(db_session_factory, tenant_id=run.tenant_id, run_id=run.id)
    assert claimed is not None
    # The process is full: no tenant slot is even asked for.
    assert await pool.claim(db_session_factory, tenant_id=run.tenant_id, run_id=run.id) is None
    pool.release()
    assert await pool.claim(db_session_factory, tenant_id=run.tenant_id, run_id=run.id)

    empty = WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=5)
    other = await _run(db_session_factory, key="op-empty")
    assert await empty.claim(db_session_factory, tenant_id=other.tenant_id, run_id=other.id) is None
    # Refused by the tenant side (no pending worker), so the process slot came back.
    assert await empty.claim(db_session_factory, tenant_id=run.tenant_id, run_id=run.id) is None


async def test_a_lost_worker_is_requeued_until_its_attempts_run_out(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    await _worker(db_session_factory, run, 0)
    sm, tenant = db_session_factory, run.tenant_id
    claimed = await fleet.claim_worker(sm, tenant_id=tenant, run_id=run.id, cap=3)
    assert claimed is not None
    requeued = await fleet.requeue(sm, tenant_id=tenant, worker=claimed, max_attempts=2)
    assert requeued.status == "pending" and requeued.attempts == 1
    again = await fleet.claim_worker(sm, tenant_id=tenant, run_id=run.id, cap=3)
    assert again is not None
    lost = await fleet.requeue(sm, tenant_id=tenant, worker=again, max_attempts=2)
    assert (lost.status, lost.error_code) == ("failed", "worker_lost")


async def test_a_run_moves_along_its_transitions_and_records_its_end(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    sm, tenant = db_session_factory, run.tenant_id
    with pytest.raises(FleetConflict):
        await fleet.set_run_status(
            sm, tenant_id=tenant, run_id=run.id, expected_version=1, status="completed"
        )
    collecting = await fleet.set_run_status(
        sm, tenant_id=tenant, run_id=run.id, expected_version=1, status="collecting"
    )
    done = await fleet.set_run_status(
        sm, tenant_id=tenant, run_id=run.id, expected_version=collecting.version, status="completed"
    )
    assert done.status == "completed"
    with pytest.raises(FleetConflict, match="is completed"):
        await _worker(sm, done, 0)


async def test_another_tenant_cannot_read_or_move_a_run(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    worker = await _worker(db_session_factory, run, 0)
    stranger = (await _run(db_session_factory, key="op-s")).tenant_id
    with pytest.raises(FleetConflict):
        await fleet.run_status(db_session_factory, tenant_id=stranger, run_id=run.id)
    assert (
        await fleet.claim_worker(db_session_factory, tenant_id=stranger, run_id=run.id, cap=3)
        is None
    )
    with pytest.raises(FleetConflict):
        await fleet.finish(
            db_session_factory,
            tenant_id=stranger,
            worker_id=worker.id,
            expected_version=1,
            status="cancelled",
            error_code="x",
        )


async def test_the_sweep_finds_workers_past_their_deadline_or_without_a_live_lease(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    for ordinal in range(3):
        await _worker(db_session_factory, run, ordinal)
    sm, tenant = db_session_factory, run.tenant_id
    leased, unleased, other = [
        await fleet.claim_worker(sm, tenant_id=tenant, run_id=run.id, cap=3) for _ in range(3)
    ]
    assert leased and unleased and other
    store = PostgresStateStore(sm, trust_caller_clock=True)
    async with sm() as session:  # the database clock: the epoch of started_at
        clock = (await session.execute(text("SELECT now()"))).scalar_one()
    await store.acquire_lease(
        worker_slot(run, leased), holder="p", turn_id="t", now=clock, ttl=timedelta(minutes=5)
    )

    # Within the grace after a claim, nothing is stale yet.
    assert await fleet.stale_workers(sm, now=clock, grace=timedelta(minutes=1)) == []

    later = clock + timedelta(minutes=2)
    stale = await fleet.stale_workers(sm, now=later, grace=timedelta(minutes=1))
    assert {worker.id for _, worker in stale} == {unleased.id, other.id}  # leased one is alive

    past_deadline = LATER + timedelta(seconds=1)
    every = await fleet.stale_workers(sm, now=past_deadline, grace=timedelta(minutes=1))
    assert {worker.id for _, worker in every} == {leased.id, unleased.id, other.id}
    async with sm() as session:
        assert await mux_state.active_lease(session, worker_slot(run, leased)) is not None


async def test_a_worker_or_run_past_its_deadline_is_not_started(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    await _worker(db_session_factory, run, 0)
    async with db_session_factory.begin() as session:
        await session.execute(
            text(
                "UPDATE fleet_worker SET deadline_at = now() - interval '1 second' WHERE run_id = :r"
            ),
            {"r": run.id},
        )
    assert (
        await fleet.claim_worker(db_session_factory, tenant_id=run.tenant_id, run_id=run.id, cap=3)
        is None
    )
    late = await _run(db_session_factory, key="op-late")
    await _worker(db_session_factory, late, 0)
    async with db_session_factory.begin() as session:
        await session.execute(
            text("UPDATE fleet_run SET deadline_at = now() - interval '1 second' WHERE id = :r"),
            {"r": late.id},
        )
    assert (
        await fleet.claim_worker(
            db_session_factory, tenant_id=late.tenant_id, run_id=late.id, cap=3
        )
        is None
    )


async def test_a_retry_with_any_other_input_is_a_conflict_not_the_old_row(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    run = await _run(sm)
    base: dict[str, Any] = dict(
        tenant_id=run.tenant_id,
        orchestrator_account_id=run.orchestrator_account_id,
        orchestrator_session_id=run.orchestrator_session_id,
        orchestrator_binding_id=run.orchestrator_binding_id,
        platform=run.platform,
        channel_id=run.channel_id,
        thread_id=run.thread_id,
        operation_key="op-1",
        total_budget_usd=run.total_budget_usd,
        deadline_at=run.deadline_at,
        max_workers=run.max_workers,
        created_by="mcp:orchestrator",
    )
    assert await fleet.create_run(sm, **base) == run
    for change in (
        {"orchestrator_session_id": "other"},
        {"thread_id": "other"},
        {"total_budget_usd": Decimal("11")},
        {"max_workers": 5},
        {"quorum": 1},
    ):
        with pytest.raises(FleetConflict, match="exists with other"):
            await fleet.create_run(sm, **{**base, **change})

    worker = await _worker(sm, run, 0)
    same: dict[str, Any] = dict(
        tenant_id=run.tenant_id,
        run_id=run.id,
        ordinal=0,
        role="research",
        spec=SPEC,
        spec_digest="d1",
        backend="openai",
        profile="openai.persistent_workspace",
        model="gpt-6-luna",
        budget_usd=Decimal("2"),
        deadline_at=LATER,
        max_turns=3,
    )
    assert await fleet.add_worker(sm, **same) == worker
    for change in (
        {"model": "gpt-6-terra"},
        {"role": "review"},
        {"budget_usd": Decimal("3")},
        {"max_turns": 4},
        {"result_contract": {"type": "object"}},
    ):
        with pytest.raises(FleetConflict, match="exists with other"):
            await fleet.add_worker(sm, **{**same, **change})


@pytest.mark.parametrize(
    "amount", [Decimal("1.2345678"), Decimal("-1"), Decimal("NaN"), Decimal("1000000")]
)
async def test_an_amount_the_columns_cannot_store_exactly_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession], amount: Decimal
) -> None:
    with pytest.raises(FleetConflict, match="not a storable"):
        await _run(db_session_factory, budget=str(amount))
    run = await _run(db_session_factory, key="op-ok")
    with pytest.raises(FleetConflict, match="not a storable"):
        await fleet.add_worker(
            db_session_factory,
            tenant_id=run.tenant_id,
            run_id=run.id,
            ordinal=0,
            role="r",
            spec=SPEC,
            spec_digest="d",
            backend="openai",
            profile="p",
            model="m",
            budget_usd=amount,
            deadline_at=LATER,
            max_turns=1,
        )


async def test_a_caller_owned_transaction_commits_or_rolls_back_its_moves_together(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    run = await _run(sm)
    worker = await _worker(sm, run, 0)
    with pytest.raises(RuntimeError):
        async with sm.begin() as session:
            await fleet.finish(
                session,
                tenant_id=run.tenant_id,
                worker_id=worker.id,
                expected_version=1,
                status="cancelled",
                error_code="stopped",
            )
            await fleet.set_run_status(
                session,
                tenant_id=run.tenant_id,
                run_id=run.id,
                expected_version=1,
                status="cancelled",
            )
            raise RuntimeError("the wake enqueue failed")
    unchanged, (still,) = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert (unchanged.status, still.status) == ("running", "pending")

    async with sm.begin() as session:
        await fleet.finish(
            session,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=1,
            status="cancelled",
            error_code="stopped",
        )
        await fleet.set_run_status(
            session, tenant_id=run.tenant_id, run_id=run.id, expected_version=1, status="cancelled"
        )
    ended, (done,) = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert (ended.status, done.status) == ("cancelled", "cancelled")

    async with sm() as idle:
        with pytest.raises(FleetConflict, match="active transaction"):
            await fleet.run_status(idle, tenant_id=run.tenant_id, run_id=run.id)


async def test_a_worker_cannot_append_with_another_workers_session_or_lease(
    bound: tuple[Any, ...],
) -> None:
    store, run, worker_a, _, session_a, lease_a = bound
    worker_b = _status_worker("running", run_id=run.id, tenant_id=run.tenant_id, ordinal=1)
    _, session_b, lease_b, _ = await _bind(store, run, worker_b, session_id="sess_w1")
    answer = _final_message(session_b.id, "B's answer")

    with pytest.raises(FleetConflict, match="lease is not"):  # B's session and lease
        await append_result(
            store, run, worker_a, session_b, answer, lease=lease_b, cursor="c", now=NOW
        )
    with pytest.raises(FleetConflict, match="is not worker"):  # B's session, A's lease
        await append_result(
            store, run, worker_a, session_b, answer, lease=lease_a, cursor="c", now=NOW
        )
    assert await store.read_events(session_b.id) == []
    assert await store.read_events(session_a.id) == []


async def test_a_result_pointer_must_be_the_workers_own_journal_entry(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    run = await _run(sm)
    store = PostgresStateStore(sm, trust_caller_clock=True)
    started: list[FleetWorker] = []
    pointers: list[ResultPointer] = []
    for ordinal in range(2):
        await _worker(sm, run, ordinal)
        claimed = await fleet.claim_worker(sm, tenant_id=run.tenant_id, run_id=run.id, cap=3)
        assert claimed is not None
        _, session, lease, binding_id = await _bind(
            store, run, claimed, session_id=f"sess_w{ordinal}"
        )
        running = await fleet.mark_running(
            sm,
            tenant_id=run.tenant_id,
            worker_id=claimed.id,
            expected_version=claimed.version,
            binding_id=binding_id,
        )
        started.append(running)
        event = _final_message(session.id, f"answer {ordinal}")
        pointers.append(
            await append_result(
                store, run, running, session, event, lease=lease, cursor="c", now=NOW
            )
        )
    a, b = started
    own = pointers[0]
    for wrong in (
        ResultPointer(
            session_id="sess_nowhere", sequence=0, digest=own.digest, excerpt=own.excerpt
        ),
        pointers[1],  # worker B's real result
        replace(own, sequence=own.sequence + 1),
        replace(own, digest="0" * 64),
        replace(own, excerpt="something else"),
    ):
        with pytest.raises(FleetConflict):
            await fleet.record_result(
                sm,
                tenant_id=run.tenant_id,
                worker_id=a.id,
                expected_version=a.version,
                result=wrong,
            )
    _, (still, _) = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert still.status == "running" and still.result is None

    done = await fleet.record_result(
        sm, tenant_id=run.tenant_id, worker_id=a.id, expected_version=a.version, result=own
    )
    assert done.status == "succeeded" and done.result == own
    assert b.status == "running"


async def test_a_lease_successor_records_the_result_its_predecessor_appended(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    run = await _run(sm)
    await _worker(sm, run, 0)
    claimed = await fleet.claim_worker(sm, tenant_id=run.tenant_id, run_id=run.id, cap=3)
    assert claimed is not None
    store = PostgresStateStore(sm, trust_caller_clock=True)
    slot, session, lease, binding_id = await _bind(store, run, claimed)
    running = await fleet.mark_running(
        sm,
        tenant_id=run.tenant_id,
        worker_id=claimed.id,
        expected_version=claimed.version,
        binding_id=binding_id,
    )
    first = await append_result(
        store,
        run,
        running,
        session,
        _final_message(session.id, "done"),
        lease=lease,
        cursor="c",
        now=NOW,
    )
    # The first process dies before recording; its successor reconciles.
    successor = await _successor(store, slot)
    rebuilt = _final_message(session.id, "rebuilt").model_copy(update={"id": "ev_other"})
    again = await append_result(
        store,
        run,
        running,
        session,
        rebuilt,
        lease=successor,
        cursor="c",
        now=NOW + timedelta(minutes=6),
    )
    assert again == first
    done = await fleet.record_result(
        sm,
        tenant_id=run.tenant_id,
        worker_id=running.id,
        expected_version=running.version,
        result=again,
    )
    assert done.result == first


@pytest.mark.parametrize("precision", [2, 6, 28])
async def test_storable_amounts_do_not_depend_on_the_ambient_decimal_precision(
    db_session_factory: async_sessionmaker[AsyncSession], precision: int
) -> None:
    with localcontext() as context:
        context.prec = precision
        run = await _run(db_session_factory, budget="3")
        worker = await fleet.add_worker(
            db_session_factory,
            tenant_id=run.tenant_id,
            run_id=run.id,
            ordinal=0,
            role="r",
            spec=SPEC,
            spec_digest="d",
            backend="openai",
            profile="p",
            model="m",
            budget_usd=Decimal("2.450001"),
            deadline_at=LATER,
            max_turns=1,
        )
        assert (run.total_budget_usd, worker.budget_usd) == (Decimal(3), Decimal("2.450001"))
        for bad in ("1.2345678", "-1", "NaN", "1000000"):
            with pytest.raises(FleetConflict, match="not a storable"):
                await _run(db_session_factory, budget=bad, key=f"op-{bad}")


async def test_a_locked_status_read_holds_the_run_until_its_transaction_ends(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(pooled)
    async with pooled.begin() as session:
        await fleet.run_status(session, tenant_id=run.tenant_id, run_id=run.id, for_update=True)

        async def add() -> FleetWorker:
            return await _worker(pooled, run, 0)

        adding = asyncio.create_task(add())
        await asyncio.sleep(0.3)
        assert not adding.done()  # add_worker waits for the run lock
    assert (await adding).ordinal == 0


# Reviewer counterexamples, kept as regressions


@pytest.mark.parametrize("precision", [2, 6])
async def test_reviewer_legal_amounts_do_not_depend_on_ambient_precision(
    pooled: async_sessionmaker[AsyncSession], precision: int
) -> None:
    with localcontext() as context:
        context.prec = precision
        run = await _run(pooled, budget="3.123456")
        worker = await _worker(pooled, run, 0)
    assert run.total_budget_usd == Decimal("3.123456") and worker.budget_usd == Decimal("2")


async def test_reviewer_a_preview_never_becomes_or_replaces_the_result(
    bound: tuple[Any, ...],
) -> None:
    store, run, worker, slot, session, lease = bound
    original = _final_message(session.id, "authoritative answer")
    preview = original.model_copy(
        update={
            "authority": "preview",
            "id": "preview-event",
            "payload": {
                **dict(original.payload),
                "content": [{"type": "text", "text": "unverified draft"}],
            },
        }
    )
    with pytest.raises(FleetConflict, match="recorded event"):
        await append_result(
            store, run, worker, session, preview, lease=lease, cursor="preview", now=NOW
        )
    assert await store.read_events(session.id) == []  # refused before anything is written

    final = await append_result(
        store, run, worker, session, original, lease=lease, cursor="record", now=NOW
    )
    successor = await _successor(store, slot)
    again = await append_result(
        store,
        run,
        worker,
        session,
        original,
        lease=successor,
        cursor="record",
        now=NOW + timedelta(minutes=6),
    )
    events = await store.read_events(session.id)
    assert again == final and final.excerpt == "authoritative answer"
    assert len({event.id for event in events}) == len(events)


async def test_reviewer_a_valid_session_lease_cannot_install_another_runs_worker_result(
    bound: tuple[Any, ...],
) -> None:
    store, run, worker, _, session, lease = bound
    other = replace(worker, id=uuid.uuid4(), run_id=uuid.uuid4())
    with pytest.raises(FleetConflict):
        await append_result(
            store,
            run,
            other,
            session,
            _final_message(session.id, "another worker answer"),
            lease=lease,
            cursor="c1",
            now=NOW,
        )
    assert await store.read_events(session.id) == []


async def test_reviewer_publication_requires_a_real_worker_journal_pointer(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    run = await _run(sm)
    worker = await _worker(sm, run, 0)
    claimed = await fleet.claim_worker(sm, tenant_id=run.tenant_id, run_id=run.id, cap=3)
    assert claimed and claimed.id == worker.id
    running = await fleet.mark_running(
        sm,
        tenant_id=run.tenant_id,
        worker_id=worker.id,
        expected_version=claimed.version,
        binding_id="binding_worker",
    )
    with pytest.raises(FleetConflict):
        await fleet.record_result(
            sm,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=running.version,
            result=ResultPointer(
                session_id="nonexistent-foreign-session",
                sequence=99,
                digest="0" * 64,
                excerpt="foreign output",
            ),
        )


async def _journaled(store: Any, session: ResourceRef, lease: Any, text_: str, key: str) -> Event:
    """A real recorded event, as the worker's turn journals it, read back from the journal."""
    event = _final_message(session.id, text_).model_copy(update={"id": f"ev_{key}"})
    await store.append_events(
        session, [JournalEntry(source_key=key, event=event)], fence=lease, cursor=key, now=NOW
    )
    (stored,) = [e for e in await store.read_events(session.id) if e.id == f"ev_{key}"]
    return stored


async def test_reviewer_another_workers_recorded_event_is_never_relabelled_as_this_ones(
    bound: tuple[Any, ...],
) -> None:
    store, run, worker_a, _, session_a, lease_a = bound
    worker_b = _status_worker("running", run_id=run.id, tenant_id=run.tenant_id, ordinal=1)
    _, session_b, lease_b, _ = await _bind(store, run, worker_b, session_id="sess_w1")
    b_answer = await _journaled(store, session_b, lease_b, "B's answer", "native:b1")

    with pytest.raises(FleetConflict, match="from session sess_w1"):
        await append_result(
            store, run, worker_a, session_a, b_answer, lease=lease_a, cursor="c", now=NOW
        )
    assert await store.read_events(session_a.id) == []  # nothing written for A

    # Control: A's own journaled answer is A's result.
    a_answer = await _journaled(store, session_a, lease_a, "A's answer", "native:a1")
    own = await append_result(
        store, run, worker_a, session_a, a_answer, lease=lease_a, cursor="c", now=NOW
    )
    assert own.excerpt == "A's answer" and own.session_id == session_a.id


async def test_reviewer_another_workers_event_never_publishes_this_worker(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    run = await _run(sm)
    store = PostgresStateStore(sm, trust_caller_clock=True)
    started: list[tuple[FleetWorker, ResourceRef, Any]] = []
    for ordinal in range(2):
        await _worker(sm, run, ordinal)
        claimed = await fleet.claim_worker(sm, tenant_id=run.tenant_id, run_id=run.id, cap=3)
        assert claimed is not None
        _, session, lease, binding_id = await _bind(
            store, run, claimed, session_id=f"sess_w{ordinal}"
        )
        running = await fleet.mark_running(
            sm,
            tenant_id=run.tenant_id,
            worker_id=claimed.id,
            expected_version=claimed.version,
            binding_id=binding_id,
        )
        started.append((running, session, lease))
    (a, session_a, lease_a), (_, session_b, lease_b) = started
    b_answer = await _journaled(store, session_b, lease_b, "B's answer", "native:b1")
    with pytest.raises(FleetConflict):
        await append_result(store, run, a, session_a, b_answer, lease=lease_a, cursor="c", now=NOW)
    _, (still_a, _) = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert still_a.status == "running" and still_a.result is None


async def test_an_underflowing_amount_is_refused_before_the_database(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(FleetConflict, match="not a storable"):
        await _run(db_session_factory, budget="1E-1000040")
    run = await _run(db_session_factory, budget="2.4500000", key="op-trailing")
    assert run.total_budget_usd == Decimal("2.45")


async def test_a_worker_bound_to_another_binding_is_refused_before_the_write(
    bound: tuple[Any, ...],
) -> None:
    store, run, worker, _, session, lease = bound
    rebound = replace(worker, binding_id="bind_of_another_generation")
    with pytest.raises(FleetConflict, match="is not worker"):
        await append_result(
            store,
            run,
            rebound,
            session,
            _final_message(session.id, "answer"),
            lease=lease,
            cursor="c",
            now=NOW,
        )
    assert await store.read_events(session.id) == []


# The spawn path a worker driver follows, as store-side contract tests. The
# driver's provider call is the one step these leave out.


def _driver_scope(run: FleetRun) -> Scope:
    return Scope(
        tenant_id=str(run.tenant_id),
        account_id=str(run.orchestrator_account_id),
        principal_id="fleet-driver",
        authorization_id="spawn_worker",
    )


async def _launch(
    sm: async_sessionmaker[AsyncSession], store: PostgresStateStore, pool: WorkerPool, run: FleetRun
) -> tuple[FleetWorker, ResourceRef, Any, str]:
    """Pool place, private slot bound and leased, spawn operation begun."""
    claimed = await pool.claim(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert claimed is not None
    _, session, lease, binding_id = await _bind(
        store, run, claimed, session_id=f"sess_{claimed.ordinal}_{claimed.attempts}"
    )
    begun = await store.begin_operation(
        _driver_scope(run),
        key=spawn_operation_key(run.id, claimed.ordinal),
        request_digest=claimed.spec_digest,
        operation_id=f"op_{claimed.id}",
        now=NOW,
        slot=worker_slot(run, claimed),
    )
    assert begun.record.operation.status == "pending"
    return claimed, session, lease, binding_id


async def test_the_spawn_path_runs_each_worker_once_and_settles_the_run(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    run = await _run(sm, max_workers=2)
    for ordinal in range(2):
        await _worker(sm, run, ordinal)
    store = PostgresStateStore(sm, trust_caller_clock=True)
    pool = WorkerPool(max_concurrent_workers=2, max_concurrent_workers_per_tenant=2)
    scope = _driver_scope(run)

    for _ in range(2):
        claimed, session, lease, binding_id = await _launch(sm, store, pool, run)
        key = spawn_operation_key(run.id, claimed.ordinal)
        await store.claim_send(scope, key, now=NOW, fence=lease)  # this send wins
        with pytest.raises(SendClaimed):
            await store.claim_send(scope, key, now=NOW, fence=lease)  # never twice
        await store.advance_operation(scope, key, "accepted", now=NOW, fence=lease)
        running = await fleet.mark_running(
            sm,
            tenant_id=run.tenant_id,
            worker_id=claimed.id,
            expected_version=claimed.version,
            binding_id=binding_id,
        )
        answer = _final_message(session.id, f"answer {claimed.ordinal}")
        pointer = await append_result(
            store, run, running, session, answer, lease=lease, cursor="c", now=NOW
        )
        await fleet.record_result(
            sm,
            tenant_id=run.tenant_id,
            worker_id=running.id,
            expected_version=running.version,
            result=pointer,
        )
        pool.release()

    async with sm.begin() as transaction:
        settled_run, workers = await fleet.run_status(
            transaction, tenant_id=run.tenant_id, run_id=run.id, for_update=True
        )
        assert run_settled(settled_run, workers)
        collecting = await fleet.set_run_status(
            transaction,
            tenant_id=run.tenant_id,
            run_id=run.id,
            expected_version=settled_run.version,
            status="collecting",
        )
    assert [w.result.excerpt for w in workers if w.result] == ["answer 0", "answer 1"]
    assert collecting.status == "collecting"


async def test_a_worker_lost_before_its_send_is_requeued_and_sent_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    run = await _run(sm)
    await _worker(sm, run, 0)
    store = PostgresStateStore(sm, trust_caller_clock=True)
    pool = WorkerPool(max_concurrent_workers=2, max_concurrent_workers_per_tenant=3)
    lost, _, _, _ = await _launch(sm, store, pool, run)
    pool.release()  # the process died before claim_send; its lease will expire

    async with sm() as session:
        clock = (await session.execute(text("SELECT now()"))).scalar_one()
    stale = await fleet.stale_workers(
        sm, now=clock + timedelta(minutes=10), grace=timedelta(minutes=1)
    )
    assert [worker.id for _, worker in stale] == [lost.id]
    key = spawn_operation_key(run.id, lost.ordinal)
    operation = await store.get_operation(_driver_scope(run), key)
    assert operation is not None and recovery(operation.operation) == "send"  # never sent

    requeued = await fleet.requeue(sm, tenant_id=run.tenant_id, worker=lost, max_attempts=2)
    assert (requeued.status, requeued.attempts) == ("pending", 1)
    again = await pool.claim(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert again is not None and again.id == lost.id
    later = NOW + timedelta(minutes=6)
    successor = await store.acquire_lease(
        worker_slot(run, again), holder="p2", turn_id="turn_2", now=later, ttl=timedelta(minutes=5)
    )
    begun = await store.begin_operation(
        _driver_scope(run),
        key=key,
        request_digest=again.spec_digest,
        operation_id=f"op_{again.id}_retry",
        now=later,
        slot=worker_slot(run, again),
    )
    assert not begun.fresh  # the same spawn operation, not a second one
    await store.claim_send(_driver_scope(run), key, now=later, fence=successor)
    with pytest.raises(SendClaimed):
        await store.claim_send(_driver_scope(run), key, now=later, fence=successor)


async def test_a_worker_lost_after_its_send_is_reconciled_never_resent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    run = await _run(sm)
    await _worker(sm, run, 0)
    store = PostgresStateStore(sm, trust_caller_clock=True)
    pool = WorkerPool(max_concurrent_workers=2, max_concurrent_workers_per_tenant=3)
    claimed, session, lease, binding_id = await _launch(sm, store, pool, run)
    scope, key = _driver_scope(run), spawn_operation_key(run.id, claimed.ordinal)
    await store.claim_send(scope, key, now=NOW, fence=lease)
    running = await fleet.mark_running(
        sm,
        tenant_id=run.tenant_id,
        worker_id=claimed.id,
        expected_version=claimed.version,
        binding_id=binding_id,
    )
    pool.release()  # the process died mid-turn, after the send

    operation = await store.get_operation(scope, key)
    assert operation is not None and recovery(operation.operation) == "reconcile"
    later = NOW + timedelta(minutes=6)
    successor = await store.acquire_lease(
        worker_slot(run, running),
        holder="p2",
        turn_id="turn_1",
        now=later,
        ttl=timedelta(minutes=5),
    )
    assert successor.took_over
    with pytest.raises(SendClaimed):  # the successor reconciles; it can never resend
        await store.claim_send(scope, key, now=later, fence=successor)
    answer = _final_message(session.id, "finished upstream")
    pointer = await append_result(
        store, run, running, session, answer, lease=successor, cursor="c", now=later
    )
    done = await fleet.record_result(
        sm,
        tenant_id=run.tenant_id,
        worker_id=running.id,
        expected_version=running.version,
        result=pointer,
    )
    assert done.status == "succeeded" and done.attempts == 0


async def test_a_full_fleet_pool_never_takes_the_tenants_chat_slots(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    run = await _run(sm)
    for ordinal in range(3):
        await _worker(sm, run, ordinal)
    pool = WorkerPool(max_concurrent_workers=6, max_concurrent_workers_per_tenant=3)
    claimed = [await pool.claim(sm, tenant_id=run.tenant_id, run_id=run.id) for _ in range(3)]
    assert all(claimed)
    chat = TurnQueue(
        platform="discord", global_cap=None, max_queued_per_tenant=50, max_queued=500, max_wait_s=60
    )
    tickets = [chat.try_claim(run.tenant_id, cap=3) for _ in range(3)]  # tenant chat cap 3
    assert all(tickets) and chat.in_flight(run.tenant_id) == 3


async def _running_worker(
    sm: async_sessionmaker[AsyncSession], store: PostgresStateStore
) -> tuple[FleetRun, FleetWorker, Any]:
    run = await _run(sm)
    await _worker(sm, run, 0)
    claimed = await fleet.claim_worker(sm, tenant_id=run.tenant_id, run_id=run.id, cap=3)
    assert claimed is not None
    _, _, lease, binding_id = await _bind(store, run, claimed)
    running = await fleet.mark_running(
        sm,
        tenant_id=run.tenant_id,
        worker_id=claimed.id,
        expected_version=claimed.version,
        binding_id=binding_id,
    )
    return run, running, lease


async def _count_input(
    sm: async_sessionmaker[AsyncSession],
    store: PostgresStateStore,
    run: FleetRun,
    worker: FleetWorker,
    lease: Any,
    key: str,
    *,
    expected_status="running",
) -> FleetWorker:
    """One model input: its operation's winning send and the turn count, one commit."""
    scope = _driver_scope(run)
    await store.begin_operation(
        scope, key=key, request_digest="input", operation_id=key, now=NOW, slot=lease.slot
    )
    async with sm.begin() as transaction:
        await mux_state.claim_send(
            transaction, scope, key, now=NOW, clock=mux_state.fixed_clock(NOW), fence=lease
        )
        return await fleet.use_turn(
            transaction,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=worker.version,
            expected_status=expected_status,
        )


async def test_a_model_input_counts_once_with_its_winning_send(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    store = PostgresStateStore(sm, trust_caller_clock=True)
    run, worker, lease = await _running_worker(sm, store)
    key = f"fleet:{run.id}:input:{worker.ordinal}:1"
    counted = await _count_input(sm, store, run, worker, lease, key)
    assert counted.turns_used == 1

    # A retry of the same input (a re-run guard, a lease successor) loses the send
    # claim, and its count rolls back with it.
    with pytest.raises(SendClaimed):
        async with sm.begin() as transaction:
            await mux_state.claim_send(
                transaction,
                _driver_scope(run),
                key,
                now=NOW,
                clock=mux_state.fixed_clock(NOW),
                fence=lease,
            )
            await fleet.use_turn(
                transaction,
                tenant_id=run.tenant_id,
                worker_id=worker.id,
                expected_version=counted.version,
            )
    _, (after,) = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert after.turns_used == 1 and after.version == counted.version

    # The next input from a stale copy of the worker is refused, not counted.
    with pytest.raises(FleetConflict, match="at version"):
        await _count_input(sm, store, run, worker, lease, f"{key}:next")
    _, (after,) = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert after.turns_used == 1 and after.version == counted.version


async def test_concurrent_sends_of_one_input_count_it_once(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    store = PostgresStateStore(pooled, trust_caller_clock=True)
    run, worker, lease = await _running_worker(pooled, store)
    key = f"fleet:{run.id}:input:{worker.ordinal}:1"
    outcomes = await asyncio.gather(
        *(_count_input(pooled, store, run, worker, lease, key) for _ in range(2)),
        return_exceptions=True,
    )
    # The loser meets either the row's version compare-and-swap or the lost claim;
    # either way it rolls back and the input counts once.
    (won,) = [o for o in outcomes if isinstance(o, FleetWorker)]
    (lost,) = [o for o in outcomes if not isinstance(o, FleetWorker)]
    assert isinstance(lost, FleetConflict | SendClaimed)
    _, (after,) = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert after.turns_used == 1 and after.version == won.version


async def test_only_a_running_worker_counts_a_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    store = PostgresStateStore(sm, trust_caller_clock=True)
    run = await _run(sm)
    await _worker(sm, run, 0)
    starting = await fleet.claim_worker(sm, tenant_id=run.tenant_id, run_id=run.id, cap=3)
    assert starting is not None and starting.status == "starting"
    _, _, lease, _ = await _bind(store, run, starting)
    with pytest.raises(FleetConflict, match="at version"):
        await _count_input(sm, store, run, starting, lease, "in:1")
    _, (after,) = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert after.turns_used == 0 and after.version == starting.version


async def test_a_worker_cannot_use_more_than_its_max_turns(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    store = PostgresStateStore(sm, trust_caller_clock=True)
    run, worker, lease = await _running_worker(sm, store)  # max_turns 3
    for n in range(1, 4):
        worker = await _count_input(sm, store, run, worker, lease, f"in:{n}")
    assert worker.turns_used == 3
    with pytest.raises(FleetConflict, match="used its 3 turns"):
        await _count_input(sm, store, run, worker, lease, "in:4")
    with pytest.raises(FleetConflict, match="at version"):
        await fleet.use_turn(
            sm, tenant_id=run.tenant_id, worker_id=worker.id, expected_version=worker.version - 1
        )
    _, (after,) = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert after.turns_used == 3


async def test_creation_input_counts_only_with_explicit_starting_state(db_session_factory):
    sm = db_session_factory
    store = PostgresStateStore(sm, trust_caller_clock=True)
    run = await _run(sm)
    await _worker(sm, run, 0)
    worker = await fleet.claim_worker(sm, tenant_id=run.tenant_id, run_id=run.id, cap=3)
    assert worker.status == "starting" and worker.binding_id is None
    slot = worker_slot(run, worker)
    lease = await store.acquire_lease(
        slot, holder="allocation", turn_id="create-input", now=NOW, ttl=timedelta(minutes=5)
    )
    counted = await _count_input(
        sm, store, run, worker, lease, "create-input:send:0", expected_status="starting"
    )
    assert counted.turns_used == 1 and counted.status == "starting" and counted.binding_id is None
    with pytest.raises(FleetConflict):
        await _count_input(
            sm, store, run, counted, lease, "extra-input:send:0", expected_status="starting"
        )
    _, (after,) = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert after.turns_used == 1 and after.version == counted.version


async def test_claim_expires_stale_tenant_work_and_preserves_other_tenants_and_holds(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    async with sm.begin() as db:
        tenant = await make_tenant(db)
    stale = await _run(sm, tenant=tenant)
    started = await _worker(sm, stale, 0)
    pending = await _worker(sm, stale, 1)
    claimed = await fleet.claim_worker(sm, tenant_id=stale.tenant_id, run_id=stale.id, cap=1)
    assert claimed is not None and claimed.id == started.id
    other = await _run(sm, key="other")
    other_worker = await _worker(sm, other, 0)
    async with sm.begin() as db:
        await db.execute(
            text("UPDATE fleet_run SET deadline_at=now()-interval '1 second' WHERE id=:id"),
            {"id": stale.id},
        )
    # A new run of the SAME tenant must not inherit the dead worker's cap slot.
    fresh = await _run(sm, key="fresh", tenant=tenant)
    fresh_worker = await _worker(sm, fresh, 0)
    recovered = await fleet.claim_worker(sm, tenant_id=fresh.tenant_id, run_id=fresh.id, cap=1)
    assert recovered is not None and recovered.id == fresh_worker.id
    ended, rows = await fleet.run_status(sm, tenant_id=stale.tenant_id, run_id=stale.id)
    assert ended.status == "failed"
    assert {w.id for w in rows} == {started.id, pending.id}
    assert all(w.status == "failed" and w.error_code == "deadline_expired" for w in rows)
    assert all(w.result is None and w.reserved_usd == Decimal(0) for w in rows)
    assert (await fleet.run_status(sm, tenant_id=other.tenant_id, run_id=other.id))[1][
        0
    ] == other_worker
    assert await fleet.sweep_expired(sm) == (0, 0)


async def test_the_scheduler_sweep_skips_a_busy_tenant_and_sweeps_the_rest(
    pooled: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMOKE J4: the sweep took every due tenant's fleet lock in one transaction with no
    lock wait bound, so one busy tenant held up (and locked) all the others."""
    sm = pooled
    monkeypatch.setattr(fleet, "SWEEP_LOCK_TIMEOUT", "100ms")
    busy, free = await _run(sm, key="busy"), await _run(sm, key="free")
    workers = [await _worker(sm, busy, 0), await _worker(sm, free, 0)]
    async with sm.begin() as db:
        await db.execute(
            text(
                "UPDATE fleet_worker SET deadline_at=now()-interval '1 second' WHERE id = ANY(:ids)"
            ),
            {"ids": [w.id for w in workers]},
        )
    held, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        async with sm.begin() as db:
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"fleet:{busy.tenant_id}"},
            )
            held.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    try:
        await held.wait()
        async with asyncio.timeout(5):
            assert await fleet.sweep_expired(sm) == (1, 0)
    finally:
        release.set()
        await holder
    assert (await fleet.run_status(sm, tenant_id=free.tenant_id, run_id=free.id))[1][
        0
    ].error_code == "deadline_expired"
    assert await fleet.sweep_expired(sm) == (1, 0)


async def test_a_swept_worker_of_a_running_run_wakes_its_root(
    pooled: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1416 B1: a failure record deferred to the sweep must still publish the worker's
    batch, as commit_worker_transition would, or the root never resumes."""
    from daimon.core.continuity import wakes

    sm = pooled
    published: list[uuid.UUID] = []
    real = wakes.enqueue_worker_finished

    async def recorded(session, *args, run_id, **kwargs):
        published.append(run_id)
        return await real(session, *args, run_id=run_id, **kwargs)

    monkeypatch.setattr(wakes, "enqueue_worker_finished", recorded)
    run = await _run(sm, key="live")
    stuck, healthy = await _worker(sm, run, 0), await _worker(sm, run, 1)
    async with sm.begin() as db:
        await db.execute(
            text(
                "UPDATE fleet_worker SET status='starting', deadline_at=now()-interval '1 second' WHERE id=:id"
            ),
            {"id": stuck.id},
        )
    assert await fleet.sweep_expired(sm) == (1, 0)
    assert published == [run.id]
    _, rows = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert next(w for w in rows if w.id == healthy.id).status == "pending"
    assert await fleet.sweep_expired(sm) == (0, 0)
    assert published == [run.id]


@pytest.mark.parametrize("state", ["pending", "starting", "running"])
async def test_deadline_sweep_ends_only_expired_workers_without_settling_money(
    db_session_factory: async_sessionmaker[AsyncSession],
    state: str,
) -> None:
    sm = db_session_factory
    run = await _run(sm)
    worker = await _worker(sm, run, 0)
    healthy = await _worker(sm, run, 1)
    async with sm.begin() as db:
        await db.execute(
            text(
                "UPDATE fleet_worker SET status=:state, deadline_at=now()-interval '1 second', reserved_usd=1.5 WHERE id=:id"
            ),
            {"state": state, "id": worker.id},
        )
    assert await fleet.sweep_expired(sm) == (1, 0)
    retained, rows = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    done = next(w for w in rows if w.id == worker.id)
    assert retained == run
    assert done.status == "failed" and done.error_code == "deadline_expired"
    assert done.version == worker.version + 1 and done.result is None
    assert done.reserved_usd == Decimal("1.5")
    assert next(w for w in rows if w.id == healthy.id) == healthy
    assert await fleet.sweep_expired(sm) == (0, 0)


async def test_a_recorded_cancel_cause_wins_over_a_later_end_and_blocks_a_claim(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """J10b: the budget watch's `usage_unavailable` is recorded before the worker ends.
    Whatever ends it later (its own failure, the deadline sweep) keeps that cause; a
    person's stop is still a stop; a pending worker with a recorded cause is never
    started; an ended worker is never re-marked."""
    sm = db_session_factory
    run = await _run(sm)
    pending = await _worker(sm, run, 0)
    other = await _worker(sm, run, 1)
    ended = await fleet.finish(
        sm,
        tenant_id=run.tenant_id,
        worker_id=other.id,
        expected_version=other.version,
        status="failed",
        error_code="worker_lost",
    )
    marked = await fleet.record_cancel_cause(
        sm,
        tenant_id=run.tenant_id,
        worker_ids=(pending.id, ended.id),
        error_code="usage_unavailable",
    )
    assert marked == 1, "only the worker not yet ended is marked"
    assert await fleet.claim_worker(sm, tenant_id=run.tenant_id, run_id=run.id, cap=5) is None
    _, rows = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    latest = next(w for w in rows if w.id == pending.id)
    assert latest.status == "pending" and latest.error_code == "usage_unavailable"
    final = await fleet.finish(
        sm,
        tenant_id=run.tenant_id,
        worker_id=pending.id,
        expected_version=latest.version,
        status="failed",
        error_code="deadline_expired",
    )
    assert final.error_code == "usage_unavailable"
    with pytest.raises(FleetConflict):
        await fleet.record_cancel_cause(
            sm, tenant_id=run.tenant_id, worker_ids=(pending.id,), error_code="deadline_expired"
        )


async def test_only_the_watch_s_own_cancels_are_recorded_and_a_stop_is_still_a_stop(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#1558 review: a worker the watch cancelled earlier for a budget cap keeps
    cap_exceeded (it is never recorded usage_unavailable); one recorded
    usage_unavailable and then stopped by a person ends stopped."""
    from daimon.core.channel_budget import WorkerCancel, cancel_workers
    from daimon.core.fleet_orchestration import cancelled_for

    sm = db_session_factory
    run = await _run(sm)
    capped = await _worker(sm, run, 0)
    blinded = await _worker(sm, run, 1)
    stopped = await _worker(sm, run, 2)
    events = {capped.id: WorkerCancel(), blinded.id: WorkerCancel(), stopped.id: WorkerCancel()}
    cancel_workers({capped.id: events[capped.id]}, "budget", run_id=run.id)
    cancel_workers(events, "usage_unavailable", run_id=run.id)
    chosen = cancelled_for(events, "usage_unavailable")
    assert set(chosen) == {blinded.id, stopped.id}
    assert (
        await fleet.record_cancel_cause(
            sm, tenant_id=run.tenant_id, worker_ids=chosen, error_code="usage_unavailable"
        )
        == 2
    )
    _, rows = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    latest = {w.id: w for w in rows}
    ended = await fleet.finish(
        sm,
        tenant_id=run.tenant_id,
        worker_id=capped.id,
        expected_version=latest[capped.id].version,
        status="failed",
        error_code="cap_exceeded",
    )
    assert ended.error_code == "cap_exceeded", "a budget cause is never overwritten"
    asked = await fleet.request_worker_stop(
        sm,
        tenant_id=run.tenant_id,
        worker_id=stopped.id,
        expected_version=latest[stopped.id].version,
    )
    halted = await fleet.finish(
        sm,
        tenant_id=run.tenant_id,
        worker_id=stopped.id,
        expected_version=asked.version,
        status="cancelled",
        error_code="stop_requested",
    )
    assert (halted.status, halted.error_code) == ("cancelled", "stop_requested")
