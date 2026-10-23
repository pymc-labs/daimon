"""Fleet runs and workers: durable rows behind `daimon.core.fleet`.

Every write is one conditional UPDATE: the row's `version` must match, and its
status must be one the target may follow (`WORKER_TRANSITIONS` /
`RUN_TRANSITIONS`). A lost race or a refused move raises `FleetConflict` and
changes nothing. Each call takes a sessionmaker (it commits on its own) or the
caller's session inside its transaction (it joins it). Creating a run or a
worker again under the same key or ordinal returns the original only for the
identical request.

The tenant concurrency cap is counted from these rows under a per-tenant
advisory lock, so it survives restarts and can never drift. It allocates pool
places only. Money is not this module's: it validates that amounts are stored
exactly, and the reservation and observed columns belong to the budget owner.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any, Literal, cast

from daimon.core._models import Account, Tenant
from daimon.core._models import FleetRun as FleetRunRow
from daimon.core._models import FleetWorker as FleetWorkerRow
from daimon.core.fleet import (
    RUN_TRANSITIONS,
    TERMINAL,
    WORKER_TRANSITIONS,
    FleetConflict,
    FleetRun,
    FleetWorker,
    ResultPointer,
    RunStatus,
    WorkerStatus,
    check_result,
    holds_slot,
    worker_slot,
)
from daimon.core.stores import mux_state, read_persisted_columns, task_continuations, tenant_ledger
from pydantic import JsonValue
from sqlalchemy import CursorResult, Row, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ACTIVE: tuple[WorkerStatus, ...] = ("starting", "running")
_MICRO_EXPONENT = -6  # whole micro-dollars: Numeric(12, 6)
_MAX_AMOUNT = Decimal("999999.999999")

type Source = async_sessionmaker[AsyncSession] | AsyncSession
"""A sessionmaker (the call commits on its own) or a caller's session inside its
transaction (the call joins it, so a status change and, say, a wake enqueue
commit together)."""


@asynccontextmanager
async def _transaction(source: Source) -> AsyncIterator[AsyncSession]:
    if isinstance(source, AsyncSession):
        if not source.in_transaction():
            raise FleetConflict("a caller-owned fleet session needs an active transaction")
        yield source
    else:
        async with source.begin() as session:
            yield session


def _amount(value: Decimal) -> Decimal:
    """An amount the columns store exactly: finite, at least 0, whole micro-dollars.

    Decided from the number's own digits, without arithmetic, so neither the
    ambient decimal precision nor an underflow can change the answer.
    """
    if not value.is_finite() or not 0 <= value <= _MAX_AMOUNT:
        raise FleetConflict(f"{value} is not a storable fleet amount")
    _, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)  # finite
    trailing_zeros = len(digits) - len("".join(map(str, digits)).rstrip("0"))
    if any(digits) and exponent + trailing_zeros < _MICRO_EXPONENT:
        raise FleetConflict(f"{value} is not a storable fleet amount")
    return value


def _run(row: FleetRunRow | Row[Any]) -> FleetRun:
    return FleetRun(
        id=row.id,
        tenant_id=row.tenant_id,
        orchestrator_account_id=row.orchestrator_account_id,
        orchestrator_session_id=row.orchestrator_session_id,
        orchestrator_binding_id=row.orchestrator_binding_id,
        platform=row.platform,
        channel_id=row.channel_id,
        thread_id=row.thread_id,
        status=cast(RunStatus, row.status),
        total_budget_usd=row.total_budget_usd,
        reserved_usd=row.reserved_usd,
        observed_usd=row.observed_usd,
        deadline_at=row.deadline_at,
        max_workers=row.max_workers,
        quorum=row.quorum,
        version=row.version,
    )


def _worker(row: FleetWorkerRow | Row[Any]) -> FleetWorker:
    result = (
        ResultPointer(
            session_id=row.result_session_id,
            sequence=row.result_sequence,
            digest=row.result_digest,
            excerpt=row.result_excerpt or "",
        )
        if row.result_session_id is not None
        and row.result_sequence is not None
        and row.result_digest is not None
        else None
    )
    return FleetWorker(
        id=row.id,
        run_id=row.run_id,
        tenant_id=row.tenant_id,
        ordinal=row.ordinal,
        role=row.role,
        spec=row.spec,
        spec_digest=row.spec_digest,
        backend=row.backend,
        profile=row.profile,
        model=row.model,
        binding_id=row.binding_id,
        status=cast(WorkerStatus, row.status),
        budget_usd=row.budget_usd,
        reserved_usd=row.reserved_usd,
        deadline_at=row.deadline_at,
        max_turns=row.max_turns,
        turns_used=row.turns_used,
        attempts=row.attempts,
        reviewer_of=row.reviewer_of,
        result_contract=row.result_contract,
        result=result,
        error_code=row.error_code,
        version=row.version,
    )


def _check_intent(row: FleetRunRow | FleetWorkerRow, intent: Mapping[str, Any], what: str) -> None:
    changed = sorted(key for key, value in intent.items() if getattr(row, key) != value)
    if changed:
        raise FleetConflict(f"{what} exists with other {', '.join(changed)}")


def _worker_from(target: WorkerStatus) -> list[str]:
    return [status for status, nexts in WORKER_TRANSITIONS.items() if target in nexts]


def _run_from(target: RunStatus) -> list[str]:
    return [status for status, nexts in RUN_TRANSITIONS.items() if target in nexts]


async def create_run(
    sessionmaker: Source,
    *,
    tenant_id: uuid.UUID,
    orchestrator_account_id: uuid.UUID,
    orchestrator_session_id: str,
    orchestrator_binding_id: str,
    platform: str,
    channel_id: str,
    thread_id: str,
    operation_key: str,
    total_budget_usd: Decimal,
    deadline_at: datetime,
    max_workers: int,
    created_by: str,
    quorum: int | None = None,
) -> FleetRun:
    """A new running fleet run, or the same one again under the tenant's key.

    The key binds the whole request: a retry with any other input raises
    `FleetConflict` rather than returning a run made for something else.
    """
    intent: dict[str, Any] = {
        "orchestrator_account_id": orchestrator_account_id,
        "orchestrator_session_id": orchestrator_session_id,
        "orchestrator_binding_id": orchestrator_binding_id,
        "platform": platform,
        "channel_id": channel_id,
        "thread_id": thread_id,
        "total_budget_usd": _amount(total_budget_usd),
        "deadline_at": deadline_at,
        "max_workers": max_workers,
        "quorum": quorum,
        "created_by": created_by,
    }
    async with _transaction(sessionmaker) as session:
        await session.execute(
            insert(FleetRunRow)
            .values(
                # Derived from the tenant's unique key (a retry hits the same row anyway),
                # so creating a run draws no random id.
                id=uuid.uuid5(tenant_id, "fleet-run:" + operation_key),
                tenant_id=tenant_id,
                operation_key=operation_key,
                status="running",
                **intent,
            )
            .on_conflict_do_nothing(constraint="uq_fleet_run_operation")
        )
        row = (
            await session.execute(
                select(FleetRunRow).where(
                    FleetRunRow.tenant_id == tenant_id, FleetRunRow.operation_key == operation_key
                )
            )
        ).scalar_one()
        _check_intent(row, intent, f"run {operation_key}")
        return _run(row)


async def add_worker(
    sessionmaker: Source,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    ordinal: int,
    role: str,
    spec: Mapping[str, JsonValue],
    spec_digest: str,
    backend: str,
    profile: str,
    model: str,
    budget_usd: Decimal,
    deadline_at: datetime,
    max_turns: int,
    reviewer_of: uuid.UUID | None = None,
    result_contract: Mapping[str, JsonValue] | None = None,
) -> FleetWorker:
    """A pending worker at `ordinal`, or the same one again; any other input there conflicts.

    The run must be running, fewer than `max_workers` of its workers may hold a slot
    (a spawn refused at admission frees its slot, see `holds_slot`), the ordinal is
    below twice `max_workers` so refusals stay bounded, and the worker's deadline is
    no later than the run's.
    """
    intent: dict[str, Any] = {
        "role": role,
        "spec": dict(spec),
        "spec_digest": spec_digest,
        "backend": backend,
        "profile": profile,
        "model": model,
        "budget_usd": _amount(budget_usd),
        "deadline_at": deadline_at,
        "max_turns": max_turns,
        "reviewer_of": reviewer_of,
        "result_contract": dict(result_contract) if result_contract is not None else None,
    }
    async with _transaction(sessionmaker) as session:
        run = (
            await session.execute(
                select(FleetRunRow)
                .where(FleetRunRow.tenant_id == tenant_id, FleetRunRow.id == run_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            raise FleetConflict(f"no fleet run {run_id} in this tenant")
        existing = (
            await session.execute(
                select(FleetWorkerRow).where(
                    FleetWorkerRow.run_id == run_id, FleetWorkerRow.ordinal == ordinal
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            _check_intent(existing, intent, f"worker {ordinal} of run {run_id}")
            return _worker(existing)
        if run.status != "running":
            raise FleetConflict(f"run {run_id} is {run.status}")
        if not 0 <= ordinal < 2 * run.max_workers:
            raise FleetConflict(
                f"run {run_id} allows {run.max_workers} workers", reason="worker_limit"
            )
        rows = (
            await session.scalars(select(FleetWorkerRow).where(FleetWorkerRow.run_id == run_id))
        ).all()
        if (
            sum(1 for other in rows if other.started_at is not None or holds_slot(_worker(other)))
            >= run.max_workers
        ):
            raise FleetConflict(
                f"run {run_id} allows {run.max_workers} workers", reason="worker_limit"
            )
        if deadline_at > run.deadline_at:
            raise FleetConflict("a worker's deadline cannot outlast its run")
        row = FleetWorkerRow(
            id=uuid.uuid4(),
            run_id=run_id,
            tenant_id=tenant_id,
            ordinal=ordinal,
            status="pending",
            **intent,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return _worker(row)


async def sweep_expired(
    sessionmaker: Source, *, tenant_id: uuid.UUID | None = None
) -> tuple[int, int]:
    """End expired work through the state owners, without settling unknown usage.

    Claims share the tenant lock with this sweep. Native termination and money
    remain their respective owners' responsibility; no result or released hold
    is inferred from a deadline.
    """
    workers_ended = runs_ended = 0
    async with _transaction(sessionmaker) as session:
        expired_worker = (
            select(FleetWorkerRow.id)
            .where(
                FleetWorkerRow.run_id == FleetRunRow.id,
                FleetWorkerRow.tenant_id == FleetRunRow.tenant_id,
                FleetWorkerRow.status.not_in(TERMINAL),
                or_(
                    FleetWorkerRow.deadline_at <= func.now(), FleetRunRow.deadline_at <= func.now()
                ),
            )
            .exists()
        )
        due = or_(
            FleetRunRow.status.in_(tuple(RUN_TRANSITIONS))
            & (FleetRunRow.deadline_at <= func.now()),
            expired_worker,
        )
        tenants = (
            [tenant_id]
            if tenant_id is not None
            else list(
                (
                    await session.scalars(
                        select(FleetRunRow.tenant_id)
                        .where(due)
                        .distinct()
                        .order_by(FleetRunRow.tenant_id)
                    )
                ).all()
            )
        )
        for tenant in tenants:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"fleet:{tenant}"},
            )
            runs = (
                await session.execute(
                    select(FleetRunRow, (FleetRunRow.deadline_at <= func.now()).label("expired"))
                    .where(FleetRunRow.tenant_id == tenant, due)
                    .order_by(FleetRunRow.id)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for run, expired in runs:
                workers = (
                    await session.scalars(
                        select(FleetWorkerRow)
                        .where(
                            FleetWorkerRow.tenant_id == tenant,
                            FleetWorkerRow.run_id == run.id,
                            FleetWorkerRow.status.not_in(TERMINAL),
                            or_(
                                FleetWorkerRow.deadline_at <= func.now(),
                                expired,
                            ),
                        )
                        .order_by(FleetWorkerRow.id)
                        .with_for_update()
                    )
                ).all()
                for worker in workers:
                    await finish(
                        session,
                        tenant_id=tenant,
                        worker_id=worker.id,
                        expected_version=worker.version,
                        status="failed",
                        error_code="deadline_expired",
                    )
                    workers_ended += 1
                if expired and run.status in RUN_TRANSITIONS:
                    await set_run_status(
                        session,
                        tenant_id=tenant,
                        run_id=run.id,
                        expected_version=run.version,
                        status="failed",
                    )
                    runs_ended += 1
    return workers_ended, runs_ended


async def claim_worker(
    sessionmaker: Source,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    cap: int,
    worker_id: uuid.UUID | None = None,
) -> FleetWorker | None:
    """The run's lowest pending worker, now `starting`, if the tenant has room.

    A worker or run past its deadline is not started; the sweep ends it. This is
    pool allocation only, not financial admission: a paid dispatch must first pass
    the budget owner's admission.

    The tenant's starting and running workers are counted under a per-tenant
    advisory lock, so concurrent claims (any process) never exceed `cap`.
    """
    async with _transaction(sessionmaker) as session:
        await sweep_expired(session, tenant_id=tenant_id)
        active = (
            await session.execute(
                select(func.count())
                .select_from(FleetWorkerRow)
                .where(FleetWorkerRow.tenant_id == tenant_id, FleetWorkerRow.status.in_(_ACTIVE))
            )
        ).scalar_one()
        if active >= cap:
            return None
        row = (
            await session.execute(
                update(FleetWorkerRow)
                .where(
                    FleetWorkerRow.id
                    == select(FleetWorkerRow.id)
                    .join(
                        FleetRunRow,
                        (FleetRunRow.id == FleetWorkerRow.run_id)
                        & (FleetRunRow.tenant_id == FleetWorkerRow.tenant_id),
                    )
                    .where(
                        FleetWorkerRow.tenant_id == tenant_id,
                        FleetWorkerRow.run_id == run_id,
                        FleetWorkerRow.status == "pending",
                        or_(
                            FleetWorkerRow.error_code.is_(None),
                            FleetWorkerRow.error_code != "stop_requested",
                        ),
                        *([FleetWorkerRow.id == worker_id] if worker_id is not None else []),
                        FleetWorkerRow.deadline_at > func.now(),
                        FleetRunRow.status == "running",
                        FleetRunRow.deadline_at > func.now(),
                    )
                    .order_by(FleetWorkerRow.ordinal)
                    .limit(1)
                    .with_for_update(of=FleetWorkerRow, skip_locked=True)
                    .scalar_subquery()
                )
                .values(
                    status="starting",
                    started_at=func.now(),
                    updated_at=func.now(),
                    version=FleetWorkerRow.version + 1,
                )
                .returning(FleetWorkerRow)
            )
        ).scalar_one_or_none()
        return _worker(row) if row is not None else None


async def _move_worker(
    sessionmaker: Source,
    *,
    tenant_id: uuid.UUID,
    worker_id: uuid.UUID,
    expected_version: int,
    target: WorkerStatus,
    values: Mapping[str, Any],
) -> FleetWorker:
    async with _transaction(sessionmaker) as session:
        row = (
            await session.execute(
                update(FleetWorkerRow)
                .where(
                    FleetWorkerRow.tenant_id == tenant_id,
                    FleetWorkerRow.id == worker_id,
                    FleetWorkerRow.version == expected_version,
                    FleetWorkerRow.status.in_(_worker_from(target)),
                )
                .values(
                    status=target,
                    version=FleetWorkerRow.version + 1,
                    updated_at=func.now(),
                    **values,
                )
                .returning(FleetWorkerRow)
            )
        ).scalar_one_or_none()
    if row is None:
        raise FleetConflict(
            f"worker {worker_id} cannot become {target} at version {expected_version}"
        )
    return _worker(row)


async def mark_running(
    sessionmaker: Source,
    *,
    tenant_id: uuid.UUID,
    worker_id: uuid.UUID,
    expected_version: int,
    binding_id: str,
) -> FleetWorker:
    """`starting -> running` once the worker's session is bound."""
    return await _move_worker(
        sessionmaker,
        tenant_id=tenant_id,
        worker_id=worker_id,
        expected_version=expected_version,
        target="running",
        values={"binding_id": binding_id},
    )


async def use_turn(
    sessionmaker: Source,
    *,
    tenant_id: uuid.UUID,
    worker_id: uuid.UUID,
    expected_version: int,
    expected_status: Literal["starting", "running"] = "running",
) -> FleetWorker:
    """Count one input under the caller's explicit expected worker state.

    Input-bearing creation explicitly expects starting, before its binding is
    available. Only its first unbound input can use that state.

    Call it in the transaction that wins that input's `claim_send` (the state
    store's one `pending -> sent` compare-and-swap for the input's operation
    key). A retry or a re-run guard loses that claim and rolls this back with
    it, so each input counts exactly once, with no second record of inputs.
    """
    async with _transaction(sessionmaker) as session:
        row = (
            await session.execute(
                update(FleetWorkerRow)
                .where(
                    FleetWorkerRow.tenant_id == tenant_id,
                    FleetWorkerRow.id == worker_id,
                    FleetWorkerRow.version == expected_version,
                    FleetWorkerRow.status == expected_status,
                    or_(
                        FleetWorkerRow.status == "running",
                        (FleetWorkerRow.status == "starting")
                        & FleetWorkerRow.binding_id.is_(None)
                        & (FleetWorkerRow.turns_used == 0),
                    ),
                    FleetWorkerRow.turns_used < FleetWorkerRow.max_turns,
                )
                .values(
                    turns_used=FleetWorkerRow.turns_used + 1,
                    version=FleetWorkerRow.version + 1,
                    updated_at=func.now(),
                )
                .returning(FleetWorkerRow)
            )
        ).scalar_one_or_none()
        if row is not None:
            return _worker(row)
        current = (
            await session.execute(
                select(FleetWorkerRow).where(
                    FleetWorkerRow.tenant_id == tenant_id, FleetWorkerRow.id == worker_id
                )
            )
        ).scalar_one_or_none()
    if (
        current is not None
        and current.version == expected_version
        and current.turns_used >= current.max_turns
    ):
        raise FleetConflict(f"worker {worker_id} has used its {current.max_turns} turns")
    raise FleetConflict(f"worker {worker_id} cannot use a turn at version {expected_version}")


async def record_result(
    sessionmaker: Source,
    *,
    tenant_id: uuid.UUID,
    worker_id: uuid.UUID,
    expected_version: int,
    result: ResultPointer,
) -> FleetWorker:
    """`running -> succeeded`, pointing at the result already in the worker's journal.

    In the same transaction the pointer is checked against the store
    (`check_result`): the worker's own slot binding must name its session, and
    the entry there must be the worker's result. A pointer to another worker's
    session, or to no entry at all, is refused and nothing changes.
    """
    async with _transaction(sessionmaker) as session:
        pair = (
            await session.execute(
                select(FleetRunRow, FleetWorkerRow)
                .join(
                    FleetWorkerRow,
                    (FleetWorkerRow.run_id == FleetRunRow.id)
                    & (FleetWorkerRow.tenant_id == FleetRunRow.tenant_id),
                )
                .where(FleetWorkerRow.tenant_id == tenant_id, FleetWorkerRow.id == worker_id)
                .with_for_update(of=FleetWorkerRow)
            )
        ).one_or_none()
        if pair is None:
            raise FleetConflict(f"no fleet worker {worker_id} in this tenant")
        run, worker = _run(pair[0]), _worker(pair[1])
        binding = await mux_state.get_binding(session, worker_slot(run, worker))
        after = await mux_state.read_events(
            session, result.session_id, after=result.sequence - 1, limit=1
        )
        check_result(run, worker, binding, after[0] if after else None, result)
        return await _move_worker(
            session,
            tenant_id=tenant_id,
            worker_id=worker_id,
            expected_version=expected_version,
            target="succeeded",
            values={
                "result_session_id": result.session_id,
                "result_sequence": result.sequence,
                "result_digest": result.digest,
                "result_excerpt": result.excerpt,
                "finished_at": func.now(),
            },
        )


async def finish(
    sessionmaker: Source,
    *,
    tenant_id: uuid.UUID,
    worker_id: uuid.UUID,
    expected_version: int,
    status: WorkerStatus,
    error_code: str,
) -> FleetWorker:
    """End a worker without a result: failed, cancelled, timed out or over budget."""
    if status not in TERMINAL or status == "succeeded":
        raise FleetConflict(f"{status} is not an unsuccessful end")
    return await _move_worker(
        sessionmaker,
        tenant_id=tenant_id,
        worker_id=worker_id,
        expected_version=expected_version,
        target=status,
        values={"error_code": error_code, "finished_at": func.now()},
    )


async def requeue(
    sessionmaker: Source,
    *,
    tenant_id: uuid.UUID,
    worker: FleetWorker,
    max_attempts: int,
) -> FleetWorker:
    """Recovery for a worker lost before its turn was sent: back to `pending`.

    Past `max_attempts` it fails instead (`worker_lost`). The caller decides it was
    never sent (its spawn operation never claimed a send); a sent turn is
    reconciled, never requeued.
    """
    if worker.attempts + 1 >= max_attempts:
        return await finish(
            sessionmaker,
            tenant_id=tenant_id,
            worker_id=worker.id,
            expected_version=worker.version,
            status="failed",
            error_code="worker_lost",
        )
    return await _move_worker(
        sessionmaker,
        tenant_id=tenant_id,
        worker_id=worker.id,
        expected_version=worker.version,
        target="pending",
        values={"attempts": FleetWorkerRow.attempts + 1, "started_at": None},
    )


async def request_worker_stop(
    sessionmaker: Source,
    *,
    tenant_id: uuid.UUID,
    worker_id: uuid.UUID,
    expected_version: int,
) -> FleetWorker:
    """Record stop intent without asserting native termination or releasing a hold."""
    async with _transaction(sessionmaker) as session:
        row = (
            await session.execute(
                update(FleetWorkerRow)
                .where(
                    FleetWorkerRow.tenant_id == tenant_id,
                    FleetWorkerRow.id == worker_id,
                    FleetWorkerRow.version == expected_version,
                    FleetWorkerRow.status.in_(("pending", "starting", "running")),
                )
                .values(
                    error_code="stop_requested",
                    version=FleetWorkerRow.version + 1,
                    updated_at=func.now(),
                )
                .returning(FleetWorkerRow)
            )
        ).scalar_one_or_none()
        if row is None:
            raise FleetConflict("worker stop transition refused")
        return _worker(row)


async def set_run_status(
    sessionmaker: Source,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    expected_version: int,
    status: RunStatus,
    wake_id: uuid.UUID | None = None,
    unless_waking: bool = False,
) -> FleetRun:
    """Compare-and-set the run's status; an ended run withdraws its wakes.

    `wake_id` is the wake whose turn makes this move; its own claim is kept for
    the wake owner to settle. With `unless_waking`, another pending or claimed
    wake of this run refuses the move in the same transaction (FleetConflict),
    so a turn already owed to the root is never cancelled under it.
    """
    finished = {"finished_at": func.now()} if status not in RUN_TRANSITIONS else {}
    async with _transaction(sessionmaker) as session:
        row = (
            await session.execute(
                update(FleetRunRow)
                .where(
                    FleetRunRow.tenant_id == tenant_id,
                    FleetRunRow.id == run_id,
                    FleetRunRow.version == expected_version,
                    FleetRunRow.status.in_(_run_from(status)),
                )
                .values(
                    status=status,
                    version=FleetRunRow.version + 1,
                    updated_at=func.now(),
                    **finished,
                )
                .returning(FleetRunRow)
            )
        ).scalar_one_or_none()
        if row is None:
            raise FleetConflict(
                f"run {run_id} cannot become {status} at version {expected_version}"
            )
        run = _run(row)
        if unless_waking and await task_continuations.count_run_wakes(
            session, run=run, exclude_id=wake_id
        ):
            raise FleetConflict(f"run {run_id} has a pending wake", reason="wake_pending")
        if finished:
            await task_continuations.retire_worker_finished_rows(session, run=run, keep_id=wake_id)
    return run


async def run_status(
    sessionmaker: Source, *, tenant_id: uuid.UUID, run_id: uuid.UUID, for_update: bool = False
) -> tuple[FleetRun, tuple[FleetWorker, ...]]:
    """The run and its workers. With `for_update` (in the caller's transaction) the
    run row stays locked to its commit, so a completion check cannot race
    `add_worker` or another completion; card and status reads leave it off."""
    async with _transaction(sessionmaker) as session:
        query = select(FleetRunRow).where(
            FleetRunRow.tenant_id == tenant_id, FleetRunRow.id == run_id
        )
        run = (
            (await session.execute(query.with_for_update())).scalar_one_or_none()
            if for_update
            else (await read_persisted_columns(session, query)).one_or_none()
        )
        if run is None:
            raise FleetConflict(f"no fleet run {run_id} in this tenant")
        worker_query = (
            select(FleetWorkerRow)
            .where(FleetWorkerRow.tenant_id == tenant_id, FleetWorkerRow.run_id == run_id)
            .order_by(FleetWorkerRow.ordinal)
        )
        workers = (
            (await session.execute(worker_query)).scalars()
            if for_update
            else (await read_persisted_columns(session, worker_query)).all()
        )
        return _run(run), tuple(_worker(worker) for worker in workers)


async def stale_workers(
    sessionmaker: Source, *, now: datetime, grace: timedelta
) -> Sequence[tuple[FleetRun, FleetWorker]]:
    """Starting or running workers the recovery sweep must look at.

    A worker is stale once its deadline has passed, or when it has been active
    for longer than `grace` and its slot holds no unexpired lease (its process is
    gone). `grace` covers the gap between a claim and the driver's first lease.
    """
    async with _transaction(sessionmaker) as session:
        rows = (
            await session.execute(
                select(FleetRunRow, FleetWorkerRow)
                .join(
                    FleetWorkerRow,
                    (FleetWorkerRow.run_id == FleetRunRow.id)
                    & (FleetWorkerRow.tenant_id == FleetRunRow.tenant_id),
                )
                .where(FleetWorkerRow.status.in_(_ACTIVE))
                .order_by(FleetWorkerRow.started_at)
            )
        ).all()
        stale: list[tuple[FleetRun, FleetWorker]] = []
        for run_row, worker_row in rows:
            run, worker = _run(run_row), _worker(worker_row)
            if worker.deadline_at <= now:
                stale.append((run, worker))
                continue
            started = worker_row.started_at
            if started is not None and now - started <= grace:
                continue
            lease = await mux_state.active_lease(session, worker_slot(run, worker))
            if lease is None or lease.expires_at <= now:
                stale.append((run, worker))
        return stale


class WorkerPool:
    """The process's cap over worker turns, in front of the durable tenant cap.

    `claim` takes a process slot, then a tenant slot through `claim_worker`; a
    refused tenant claim gives the process slot back. The caller `release`s the
    process slot when the worker's turn ends, on every path. The tenant slot needs
    no release: it is the worker row's own status.
    """

    def __init__(self, *, max_concurrent_workers: int, max_concurrent_workers_per_tenant: int):
        self._slots = asyncio.Semaphore(max_concurrent_workers)
        self._tenant_cap = max_concurrent_workers_per_tenant

    async def claim(
        self,
        sessionmaker: Source,
        *,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
        worker_id: uuid.UUID | None = None,
    ) -> FleetWorker | None:
        """The run's next pending worker, now `starting`, or None if no room."""
        if self._slots.locked():
            return None
        await self._slots.acquire()
        try:
            worker = await claim_worker(
                sessionmaker,
                tenant_id=tenant_id,
                run_id=run_id,
                cap=self._tenant_cap,
                worker_id=worker_id,
            )
        except BaseException:
            self._slots.release()
            raise
        if worker is None:
            self._slots.release()
        return worker

    def release(self) -> None:
        self._slots.release()


type BudgetGraph = tuple[tuple[FleetRun, tuple[FleetWorker, ...]], ...]


async def lock_budget(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    run_id: uuid.UUID | None,
    financial: bool = True,
    persisted: bool = False,
    locking: bool = True,
) -> tuple[BudgetGraph, Mapping[uuid.UUID, datetime | None]]:
    """Join the caller's financial transaction before any fleet row transition.

    Same tenant lock as pool claims, then the financial owner's tenant row,
    actor, runs and workers. Ordinary ledger inserts use their existing foreign
    key lock; no global table lock or additional writer query is required.
    Every caller composing fleet row writers must acquire this first, before
    either a run or worker lock. Include only live/unsettled runs and the
    requested run for terminal readback. Passive refresh uses tenant KEY SHARE;
    reservation and fresh native admission retain FOR UPDATE. Return original
    allocation stamps.

    Without `locking` this is a read of the caller's snapshot: no tenant lock and no
    row locks, for a read-only transaction that must never wait behind writers.
    """
    if not session.in_transaction():
        raise FleetConflict("fleet budget requires an active transaction")
    if locking:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"fleet:{tenant_id}"},
        )
        # Same tenant/account order as audit and erasure.
        tenant = await tenant_ledger.lock_funds(session, tenant_id=tenant_id, financial=financial)
    else:
        tenant = await session.scalar(select(Tenant.id).where(Tenant.id == tenant_id))
    actor_query = select(Account.id).where(Account.id == actor_id, Account.tenant_id == tenant_id)
    actor = await session.scalar(
        actor_query.with_for_update(read=True, key_share=True) if locking else actor_query
    )
    if tenant is None or actor is None:
        raise FleetConflict("financial admission requires a live tenant actor")
    run_query = (
        select(FleetRunRow)
        .where(
            FleetRunRow.tenant_id == tenant_id,
            (FleetRunRow.id == run_id)
            | FleetRunRow.status.in_(RUN_TRANSITIONS)
            | (FleetRunRow.reserved_usd > FleetRunRow.observed_usd),
        )
        .order_by(FleetRunRow.id)
    )
    if locking:
        run_query = run_query.with_for_update()
    runs = (
        (await read_persisted_columns(session, run_query, reject_new=True)).all()
        if persisted
        else tuple(await session.scalars(run_query))
    )
    worker_query = (
        select(FleetWorkerRow)
        .where(
            FleetWorkerRow.tenant_id == tenant_id,
            FleetWorkerRow.run_id.in_(run.id for run in runs),
        )
        .order_by(FleetWorkerRow.id)
    )
    if locking:
        worker_query = worker_query.with_for_update()
    workers = (
        (await read_persisted_columns(session, worker_query, reject_new=True)).all()
        if persisted
        else tuple(await session.scalars(worker_query))
    )
    return (
        tuple(
            (_run(run), tuple(_worker(w) for w in workers if w.run_id == run.id)) for run in runs
        ),
        {w.id: w.started_at for w in workers},
    )


def project_budget(
    graph: BudgetGraph,
    *,
    exposures: Mapping[uuid.UUID, Decimal],
    debits: Mapping[uuid.UUID, Decimal],
    zero_spend: frozenset[uuid.UUID] = frozenset(),
) -> BudgetGraph:
    """What `save_budget` would persist, computed without writing, for a snapshot read.

    The same release rule applies; run sums use the same exact stored amounts.
    """
    result: list[tuple[FleetRun, tuple[FleetWorker, ...]]] = []
    for run, workers in graph:
        projected: list[FleetWorker] = []
        for worker in workers:
            amount = _amount(exposures[worker.id])
            if amount < worker.reserved_usd and worker.id not in zero_spend:
                raise FleetConflict("unknown worker exposure cannot be released")
            projected.append(replace(worker, reserved_usd=amount))
        with localcontext() as arithmetic:
            arithmetic.prec = 80  # exact for stored micro-dollar amounts, as SQL sums are
            total = sum((worker.reserved_usd for worker in projected), Decimal(0))
        result.append(
            (
                replace(run, reserved_usd=total, observed_usd=_amount(debits[run.id])),
                tuple(projected),
            )
        )
    return tuple(result)


async def save_budget(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    graph: BudgetGraph,
    exposures: Mapping[uuid.UUID, Decimal],
    debits: Mapping[uuid.UUID, Decimal],
    zero_spend: frozenset[uuid.UUID] = frozenset(),
) -> BudgetGraph:
    """Persist exposure selected by the sole monetary producer.

    Caller has lock_budget's transaction/graph. Run sums are derived in SQL,
    with the same exact Numeric basis as the rows, never ambient Decimal math.
    No status, result, usage outbox or tenant debit is changed here.
    Only the producer's proven never-allocated evidence class may decrease.
    """
    if not session.in_transaction():
        raise FleetConflict("fleet budget requires an active transaction")
    for run, workers in graph:
        for worker in workers:
            amount = _amount(exposures[worker.id])
            if amount < worker.reserved_usd and worker.id not in zero_spend:
                raise FleetConflict("unknown worker exposure cannot be released")
            if amount == worker.reserved_usd:
                continue
            changed = await session.scalar(
                update(FleetWorkerRow)
                .where(
                    FleetWorkerRow.tenant_id == tenant_id,
                    FleetWorkerRow.id == worker.id,
                    FleetWorkerRow.version == worker.version,
                )
                .values(
                    reserved_usd=amount, version=FleetWorkerRow.version + 1, updated_at=func.now()
                )
                .returning(FleetWorkerRow.id)
            )
            if changed is None:
                raise FleetConflict("worker changed during monetary update")
        total = await session.scalar(
            select(func.coalesce(func.sum(FleetWorkerRow.reserved_usd), 0)).where(
                FleetWorkerRow.tenant_id == tenant_id, FleetWorkerRow.run_id == run.id
            )
        )
        observed = _amount(debits[run.id])
        if total == run.reserved_usd and observed == run.observed_usd:
            continue
        changed = await session.scalar(
            update(FleetRunRow)
            .where(
                FleetRunRow.tenant_id == tenant_id,
                FleetRunRow.id == run.id,
                FleetRunRow.version == run.version,
            )
            .values(
                reserved_usd=total,
                observed_usd=observed,
                version=FleetRunRow.version + 1,
                updated_at=func.now(),
            )
            .returning(FleetRunRow.id)
        )
        if changed is None:
            raise FleetConflict("run changed during monetary update")
    # Already-held locks are retained; do not acquire the funds lock twice.
    result: list[tuple[FleetRun, tuple[FleetWorker, ...]]] = []
    for run, _ in graph:
        result.append(await run_status(session, tenant_id=tenant_id, run_id=run.id))
    return tuple(result)


_OPEN = ("running", "collecting")


def _decision(raw: object) -> dict[str, str] | None:
    """A stored run decision; malformed entries read "skip" (never widen)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): (value if value in ("share", "no_answer") else "skip")
        for key, value in cast(dict[object, object], raw).items()
    }


async def decide_run_logins(
    sessionmaker: Source,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    requester_account_id: uuid.UUID,
    decision: Mapping[str, Literal["share", "skip", "no_answer"]],
) -> bool:
    """Set the run's login decision once, from null, for the run's own requester.

    False when it is already decided (a later click never changes it), the run is
    another person's, or it already ended.
    """
    async with _transaction(sessionmaker) as session:
        result = await session.execute(
            update(FleetRunRow)
            .where(
                FleetRunRow.tenant_id == tenant_id,
                FleetRunRow.id == run_id,
                FleetRunRow.orchestrator_account_id == requester_account_id,
                FleetRunRow.login_decision.is_(None),
                FleetRunRow.status.in_(_OPEN),
            )
            .values(login_decision=dict(decision))
        )
        return cast(CursorResult[Any], result).rowcount == 1


async def run_login_decision(
    sessionmaker: Source, *, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> dict[str, str] | None:
    """The run's login decision, or None while undecided. Malformed entries read skip."""
    async with _transaction(sessionmaker) as session:
        raw = await session.scalar(
            select(FleetRunRow.login_decision).where(
                FleetRunRow.tenant_id == tenant_id, FleetRunRow.id == run_id
            )
        )
    return _decision(raw)


async def open_login_question(
    sessionmaker: Source,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    requester_account_id: uuid.UUID,
    question: Mapping[str, JsonValue],
) -> dict[str, Any] | None:
    """Open the run's one login question (from null), or return the one already open.

    None when the run is decided, ended, or another person's: nothing to ask.
    """
    async with _transaction(sessionmaker) as session:
        row = (
            await session.execute(
                select(FleetRunRow)
                .where(
                    FleetRunRow.tenant_id == tenant_id,
                    FleetRunRow.id == run_id,
                    FleetRunRow.orchestrator_account_id == requester_account_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.status not in _OPEN or row.login_decision is not None:
            return None
        if row.login_question is None:
            row.login_question = dict(question)
        return dict(cast(dict[str, Any], row.login_question))


async def lock_login_state(
    session: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[uuid.UUID, str, dict[str, Any] | None, dict[str, str] | None] | None:
    """In the caller's transaction: lock the run and read (requester, status, question,
    decision). None when there is no such run."""
    row = (
        await session.execute(
            select(FleetRunRow)
            .where(FleetRunRow.tenant_id == tenant_id, FleetRunRow.id == run_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    question = row.login_question
    return (
        row.orchestrator_account_id,
        row.status,
        dict(cast(dict[str, Any], question)) if isinstance(question, dict) else None,
        _decision(row.login_decision),
    )


async def write_login_state(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    question: Mapping[str, JsonValue],
    decision: Mapping[str, str] | None = None,
) -> None:
    """In the caller's transaction (after `lock_login_state`): save the question, and
    the decision only if the run has none yet (never changed once set)."""
    values: dict[str, Any] = {"login_question": dict(question)}
    query = update(FleetRunRow).where(FleetRunRow.tenant_id == tenant_id, FleetRunRow.id == run_id)
    await session.execute(query.values(**values))
    if decision is not None:
        await session.execute(
            query.where(FleetRunRow.login_decision.is_(None)).values(login_decision=dict(decision))
        )


async def open_login_questions(
    sessionmaker: Source, *, platform: str
) -> tuple[tuple[uuid.UUID, uuid.UUID, dict[str, Any]], ...]:
    """Every undecided login question of a running run on this platform:
    (tenant id, run id, question)."""
    async with _transaction(sessionmaker) as session:
        rows = (
            await session.execute(
                select(FleetRunRow.tenant_id, FleetRunRow.id, FleetRunRow.login_question).where(
                    FleetRunRow.platform == platform,
                    FleetRunRow.status.in_(_OPEN),
                    FleetRunRow.login_question.is_not(None),
                    FleetRunRow.login_decision.is_(None),
                )
            )
        ).all()
    found: list[tuple[uuid.UUID, uuid.UUID, dict[str, Any]]] = []
    for row in rows:
        question: object = row.login_question
        if isinstance(question, dict):
            found.append((row.tenant_id, row.id, dict(cast(dict[str, Any], question))))
    return tuple(found)
