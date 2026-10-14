"""The durable wake queue: run a turn in an existing thread, later, at most once.

A wake is a `task_continuations` row with a lease. Anything that wants a turn
to happen in a thread without a person asking for it again — a timer, a
finished background job — enqueues one and stops there. Running it is the
chat adapter's job, through the same admit → bind → run path a mention takes,
because only the adapter holds the platform client and the turn lifecycle.

The ladder, every step one conditional UPDATE:

- `enqueue_wake` writes `pending` with `available_at`.
- `claim_wake` takes it with an owner and a lease (`WAKE_CLAIM_LEASE`).
- `start_wake` commits the fence right before the turn begins, and stretches
  the lease over the whole turn.
- `settle_wake` / `release_wake` close it out or hand it back.

Why the fence: a process can die after its turn became visible and before it
recorded that. Its lease then expires with nothing to say which side it died
on. So an expired claim is taken over only while `started_at` is NULL — the
turn cannot have begun — and one that had started is settled
`skipped/interrupted` by `abandon_interrupted_wakes` rather than run twice.
Lease expiry retries; the retry is never blind.

Delivery is a per-adapter hook: `run_wake_poller` asks the adapter to open each
thread with due work (`WakeOpener`), and the adapter runs its normal
continuation dispatch there. An adapter that registers no opener simply leaves
its rows pending, which is the safe default.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

import structlog
from daimon.core.continuity.continuation import (
    MAX_REQUESTED_WORK,
    ContinuationDecision,
    ContinuationRequest,
    require_writable_reason,
)
from daimon.core.errors import DaimonError
from daimon.core.fleet import FleetConflict, FleetRun, FleetWorker, run_settled
from daimon.core.stores import accounts, fleet, mux_state, task_continuations, thread_sessions
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.task_continuations import (
    abandon_interrupted_wake_rows,
    cancel_wake_row,
    claim_wake_row,
    defer_wake_rows,
    list_dispatchable_continuations,
    list_due_wake_rows,
    record_continuation,
    release_wake_row,
    settle_wake_row,
    start_wake_row,
)
from daimon.core.turn.ceiling import TURN_CEILING_S
from mux.contracts.ids import ChannelRef, ThreadRef
from mux.state.lease import Slot
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

__all__ = [
    "WAKE_CLAIM_LEASE",
    "WAKE_MAX_ATTEMPTS",
    "WAKE_POLL_INTERVAL_S",
    "WAKE_RETRY_DELAY",
    "WAKE_RUN_LEASE",
    "WAKE_UNAVAILABLE_DELAY",
    "FrozenWorkerBatch",
    "WorkerFinishedRootChanged",
    "WakeClaim",
    "WakeOpener",
    "WakeThread",
    "abandon_interrupted_wakes",
    "arm_worker_finished",
    "busy_retry_at",
    "cancel_wake",
    "claim_wake",
    "decide_worker_finished",
    "require_worker_finished_root",
    "enqueue_wake",
    "enqueue_worker_finished",
    "list_dispatchable_wakes",
    "list_due_wake_threads",
    "poll_wakes_once",
    "release_wake",
    "run_wake_poller",
    "settle_wake",
    "skip_thread_wakes",
    "start_wake",
    "worker_finished_key",
]

#: How long a claim may sit before its turn starts. Covers the decision reads
#: and the platform history fetch, nothing slow.
WAKE_CLAIM_LEASE: Final[timedelta] = timedelta(minutes=5)

#: The lease once the fence is committed: the whole turn, plus room for the
#: settle write. After this a started row is `interrupted`, never re-run.
WAKE_RUN_LEASE: Final[timedelta] = timedelta(seconds=TURN_CEILING_S) + timedelta(minutes=5)

#: Claims per row before it settles `attempts_exhausted`. Only claims that were
#: lost to a crash count (a release refunds its claim), so a row that keeps
#: killing its process stops eventually while a busy thread can wait forever.
WAKE_MAX_ATTEMPTS: Final[int] = 5

#: How far a released wake is pushed back — long enough for the turn that
#: blocked it to finish.
WAKE_RETRY_DELAY: Final[timedelta] = timedelta(seconds=30)

#: How far a thread the adapter could not open is pushed back, so the poll
#: moves on to other threads instead of offering the same ones every time.
WAKE_UNAVAILABLE_DELAY: Final[timedelta] = timedelta(minutes=5)

WAKE_POLL_INTERVAL_S: Final[float] = 15.0

_POLL_BATCH: Final[int] = 100


class WakeClaim(BaseModel):
    """Proof of holding one row's lease. Every later write is guarded on `owner`."""

    model_config = ConfigDict(frozen=True)

    idempotency_key: uuid.UUID
    owner: str
    attempts: int


class WakeThread(BaseModel):
    """A thread with due wake work, addressed the way an adapter opens it.

    `requester_account_id` is the oldest due row's requester, for adapters
    that read the thread's live session per account before dispatching.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    platform: str
    parent_channel_id: str
    thread_id: str
    requester_account_id: uuid.UUID


#: The adapter hook: start the adapter's continuation dispatch in this thread.
#: Should spawn the dispatch and return, so one long turn does not hold up the
#: poll; the dispatch must take the thread's turn guard. Returns True when the
#: dispatch was started (or the thread was settled with `skip_thread_wakes`
#: because it is gone), False when the thread could not be opened this time —
#: no token, archived workspace, a platform error. A False (or a raise) pushes
#: the thread's rows back by `WAKE_UNAVAILABLE_DELAY`.
WakeOpener = Callable[[WakeThread], Awaitable[bool]]


async def _record_wake(
    session: AsyncSession, request: ContinuationRequest, *, available_at: datetime
) -> None:
    await record_continuation(
        session,
        tenant_id=request.tenant_id,
        platform=request.platform,
        parent_channel_id=request.parent_channel_id,
        thread_id=request.thread_id,
        requester_account_id=request.requester_account_id,
        requester_external_user_id=request.requester_external_user_id,
        target_ma_agent_id=request.target_ma_agent_id,
        target_name=request.target_name,
        reason=request.reason,
        idempotency_key=request.idempotency_key,
        requested_work=request.requested_work,
        available_at=available_at,
    )


async def enqueue_wake(
    sessionmaker: async_sessionmaker[AsyncSession] | AsyncSession,
    request: ContinuationRequest,
    *,
    available_at: datetime,
) -> None:
    """Queue work, using an existing transaction when supplied.

    A factory retains the existing independent commit. A session must already
    hold a transaction: its owning state transition commits or rolls back both
    that transition and the wake, with no commit inside this function.
    """
    require_writable_reason(request.reason)
    if isinstance(sessionmaker, AsyncSession):
        if not sessionmaker.in_transaction():
            raise ValueError("Enqueuing with a session requires an active transaction")
        await _record_wake(sessionmaker, request, available_at=available_at)
        return
    async with sessionmaker.begin() as session:
        await _record_wake(session, request, available_at=available_at)


# A finite timestamp avoids asyncpg's special infinity/datetime.max conversion.
_UNREADY_AT: Final = datetime(9999, 1, 1, tzinfo=UTC)


def worker_finished_key(
    run_id: uuid.UUID, worker_ids: Sequence[uuid.UUID] | None = None
) -> uuid.UUID:
    """A retry-stable identity for one exact batch; None retains the legacy key."""
    suffix = ""
    if worker_ids is not None:
        if not worker_ids or len(set(worker_ids)) != len(worker_ids):
            raise ValueError("A worker batch must be nonempty and unique")
        suffix = ":" + ",".join(str(key) for key in sorted(worker_ids, key=str))
    return uuid.uuid5(run_id, "worker_finished" + suffix)


class FrozenWorkerBatch(BaseModel):
    """Immutable wait facts; readiness does not certify successful results."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: uuid.UUID
    worker_ids: tuple[uuid.UUID, ...]
    quorum: int
    binding_id: str
    session_id: str


def _batch(run: FleetRun, workers: Sequence[FleetWorker], ids: Sequence[uuid.UUID]):
    selected = {worker.id: worker for worker in workers}
    if not ids or len(set(ids)) != len(ids) or not set(ids) <= selected.keys():
        raise ValueError("Batch workers must be unique members of this run")
    reviews = {selected[key].reviewer_of is not None for key in ids}
    if len(reviews) != 1:
        raise ValueError("Initial and review workers require separate batches")
    return FrozenWorkerBatch(
        run_id=run.id,
        worker_ids=tuple(sorted(ids, key=str)),
        quorum=len(ids) if True in reviews else min(run.quorum or len(ids), len(ids)),
        binding_id=run.orchestrator_binding_id,
        session_id=run.orchestrator_session_id,
    )


def _address_matches(run: FleetRun, request: ContinuationRequest | TaskContinuationRow) -> bool:
    return (
        request.reason == "worker_finished"
        and request.tenant_id == run.tenant_id
        and request.requester_account_id == run.orchestrator_account_id
        and request.platform == run.platform
        and request.parent_channel_id == run.channel_id
        and request.thread_id == run.thread_id
    )


def _retained_batch(run: FleetRun, workers: Sequence[FleetWorker], row: TaskContinuationRow):
    batch = FrozenWorkerBatch.model_validate_json(row.requested_work or "")
    if (
        batch.run_id != run.id
        or not _address_matches(run, row)
        or batch != _batch(run, workers, batch.worker_ids)
        or row.requested_work != batch.model_dump_json()
        or row.idempotency_key != worker_finished_key(run.id, batch.worker_ids)
    ):
        raise ValueError("Frozen batch intent differs from its original run")
    return batch


class WorkerFinishedRootChanged(DaimonError):
    """The completion turn cannot send to its exact original root."""


async def decide_worker_finished(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: TaskContinuationRow,
    now: datetime,
) -> ContinuationDecision | None:
    """Re-read the frozen root before normal runner admission; this grants no send."""
    return await _decide_worker_finished(sessionmaker, row, now)


async def require_worker_finished_root(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: TaskContinuationRow,
    *,
    session_id: str,
    account_id: uuid.UUID,
    agent_id: str,
    now: datetime,
) -> None:
    """Fence a started completion before preparation and the normal runner's send.

    Its own active marker and slot lease are expected after binding. All original
    root/intent checks remain the decision owner's; this is not authorization.
    """
    decision = await _decide_worker_finished(sessionmaker, row, now, started=True)
    if decision is not None:
        batch = FrozenWorkerBatch.model_validate_json(row.requested_work or "")
        if (session_id, account_id, agent_id) == (
            batch.session_id,
            row.requester_account_id,
            row.target_ma_agent_id,
        ):
            return
    raise WorkerFinishedRootChanged("Original fleet root is unavailable")


async def _decide_worker_finished(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: TaskContinuationRow,
    now: datetime,
    *,
    started: bool = False,
) -> ContinuationDecision | None:
    """Re-read the frozen root before normal runner admission; this grants no send.

    Missing or stale durable facts defer. The adapter retains its per-thread
    guard and repeats current authorization/binding checks before any effect.
    """
    try:
        async with sessionmaker.begin() as db:
            retained = await task_continuations.get_continuation(
                db, idempotency_key=row.idempotency_key
            )
            if retained is None or any(
                getattr(retained, key) != getattr(row, key)
                for key in (*ContinuationRequest.model_fields, "id")
            ):
                return None
            if retained.status not in {"pending", "claimed"}:
                return None
            if (retained.started_at is not None) != started:
                return None
            if started and (
                retained.status != "claimed"
                or not row.lease_owner
                or retained.lease_owner != row.lease_owner
                or retained.attempts != row.attempts
                or retained.started_at != row.started_at
                or retained.lease_expires_at is None
                or retained.lease_expires_at <= now
            ):
                return None
            if retained.available_at is None or retained.available_at > now:
                return None
            parsed = FrozenWorkerBatch.model_validate_json(retained.requested_work or "")
            run, workers = await fleet.run_status(
                db, tenant_id=row.tenant_id, run_id=parsed.run_id, for_update=True
            )
            batch = _retained_batch(run, workers, retained)
            members = [worker for worker in workers if worker.id in batch.worker_ids]
            if run.status != "running" or now >= run.deadline_at:
                return None
            if not run_settled(replace(run, quorum=batch.quorum), members):
                return None
            identity = await accounts.get_account_with_tenant(
                db, account_id=run.orchestrator_account_id
            )
            if (
                identity is None
                or identity.tenant_id != run.tenant_id
                or identity.platform != run.platform
                or identity.platform_user_id != row.requester_external_user_id
            ):
                return None
            slot = Slot(
                thread=ThreadRef(
                    channel=ChannelRef(
                        tenant_id=str(run.tenant_id),
                        platform=run.platform,
                        channel_id=run.channel_id,
                    ),
                    thread_id=run.thread_id,
                ),
                account_id=str(run.orchestrator_account_id),
            )
            binding = await mux_state.get_binding(db, slot)
            if (
                binding is None
                or binding.id != batch.binding_id
                or binding.thread != slot.thread
                or binding.legacy_account_id != slot.account_id
                or binding.native_refs.get("session") != batch.session_id
            ):
                return None
            projection = await mux_state.projection(db, batch.session_id)
            if (
                projection is None
                or projection.session.id != batch.session_id
                or projection.session.kind != "session"
                or projection.session.provider != binding.provider
                or projection.session.tenant_id not in {None, str(run.tenant_id)}
                or projection.session.account_id not in {None, slot.account_id}
                or projection.gaps
                or projection.state == "terminated"
            ):
                return None
            lease = await mux_state.active_lease(db, slot)
            live = await thread_sessions.get_live_thread_session(
                db,
                tenant_id=run.tenant_id,
                platform=run.platform,
                thread_id=run.thread_id,
                account_id=run.orchestrator_account_id,
            )
            if live is not None and (
                live.ma_session_id != batch.session_id
                or live.channel_id != run.channel_id
                or live.ma_agent_id != row.target_ma_agent_id
            ):
                return None
            if not started and live is not None and live.active_turn_message_id is not None:
                return ContinuationDecision(action="skip_turn_running")
            if not started and (
                projection.state != "idle" or projection.active_root_turn is not None or lease
            ):
                return ContinuationDecision(action="skip_turn_running")
    except (FleetConflict, ValidationError, ValueError):
        return None
    summary = ", ".join(f"{worker.id}:{worker.status}" for worker in members[:5])
    seed = (
        f"Resume fleet run {run.id}; frozen batch {row.idempotency_key} is ready "
        f"at threshold {batch.quorum}/{len(members)}. Worker state excerpt: {summary}. "
        "Re-read fleet_status and validate fleet_collect; readiness does not certify success."
    )[:MAX_REQUESTED_WORK]
    return ContinuationDecision(action="dispatch", seed_user_message=seed)


async def arm_worker_finished(
    session: AsyncSession,
    request: ContinuationRequest,
    *,
    run_id: uuid.UUID,
    worker_ids: Sequence[uuid.UUID],
    now: datetime,
) -> TaskContinuationRow | None:
    """Freeze a batch in the existing queue, then evaluate already-finished workers.

    Installation requires the compatible reader/rollback floor; no caller is
    wired by this API. The run stays running so it can admit a review cohort.
    """
    if not session.in_transaction():
        raise ValueError("Arming a worker batch requires an active transaction")
    if request.idempotency_key != worker_finished_key(run_id, worker_ids):
        raise ValueError("Worker completion requires the batch's stable wake identity")
    run, workers = await fleet.run_status(
        session, tenant_id=request.tenant_id, run_id=run_id, for_update=True
    )
    if not _address_matches(run, request):
        raise ValueError("Worker wait address differs from its original root")
    batch = _batch(run, workers, worker_ids)
    payload = batch.model_dump_json()
    row = await task_continuations.get_continuation(
        session, idempotency_key=request.idempotency_key
    )
    if row is not None:
        expected = request.model_copy(update={"requested_work": payload})
        if any(getattr(row, key) != value for key, value in expected.model_dump().items()):
            raise ValueError("Worker wait exists with a different frozen intent")
    elif run.status == "running" and now < run.deadline_at:
        row = await task_continuations.record_continuation(
            session,
            **request.model_copy(update={"requested_work": payload}).model_dump(),
            available_at=_UNREADY_AT,
        )
    else:
        return None
    await enqueue_worker_finished(session, tenant_id=run.tenant_id, run_id=run_id, now=now)
    return await task_continuations.get_continuation(session, idempotency_key=row.idempotency_key)


async def enqueue_worker_finished(
    session: AsyncSession,
    request: ContinuationRequest | None = None,
    *,
    tenant_id: uuid.UUID | None = None,
    run_id: uuid.UUID,
    now: datetime,
) -> bool:
    """Offer ready frozen batches with the caller's terminal move, without committing.

    The writer locks the run before changing its worker, then calls here in
    that same transaction.
    Deadline refusal never undoes that terminal move. Missing/corrupt waits
    authorize nothing and do not strand unrelated terminal facts.
    """
    if not session.in_transaction():
        raise ValueError("Worker completion requires an active transaction")
    if request is not None:
        if request.reason != "worker_finished" or request.idempotency_key != worker_finished_key(
            run_id
        ):
            raise ValueError("Worker completion requires the run's stable wake identity")
        return False  # Legacy unarmed writes remain closed.
    if tenant_id is None:
        raise ValueError("Publishing frozen batches requires the authorized tenant")
    run, workers = await fleet.run_status(
        session, tenant_id=tenant_id, run_id=run_id, for_update=True
    )
    if run.status != "running" or now >= run.deadline_at:
        return False
    rows = await task_continuations.list_pending_continuations(
        session,
        tenant_id=tenant_id,
        platform=run.platform,
        thread_id=run.thread_id,
        include_future=True,
    )
    published = 0
    for row in rows:
        if row.reason != "worker_finished" or row.available_at != _UNREADY_AT:
            continue
        try:
            batch = FrozenWorkerBatch.model_validate_json(row.requested_work or "")
            if batch.run_id != run_id:
                continue
            batch = _retained_batch(run, workers, row)
        except (ValidationError, ValueError):
            log.warning("wake.invalid_worker_batch", idempotency_key=str(row.idempotency_key))
            continue
        if run_settled(
            replace(run, quorum=batch.quorum),
            [worker for worker in workers if worker.id in batch.worker_ids],
        ):
            published += await task_continuations.publish_worker_finished_row(
                session,
                tenant_id=tenant_id,
                idempotency_key=row.idempotency_key,
                expected_available_at=_UNREADY_AT,
                available_at=now,
            )
    return published > 0


async def claim_wake(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    idempotency_key: uuid.UUID,
    now: datetime,
) -> WakeClaim | None:
    """Claim a due row, or take over an expired unstarted claim; None if neither.

    Each call gets its own transaction and a fresh owner, so two dispatchers —
    in one process or two — race on the row lock and exactly one wins.
    """
    owner = uuid.uuid4().hex
    async with sessionmaker.begin() as session:
        row = await claim_wake_row(
            session,
            idempotency_key=idempotency_key,
            owner=owner,
            now=now,
            lease=WAKE_CLAIM_LEASE,
            max_attempts=WAKE_MAX_ATTEMPTS,
        )
    if row is None:
        return None
    return WakeClaim(idempotency_key=idempotency_key, owner=owner, attempts=row.attempts)


async def start_wake(
    sessionmaker: async_sessionmaker[AsyncSession],
    claim: WakeClaim,
    *,
    now: datetime,
) -> bool:
    """Commit the fence before the turn's first visible effect.

    False means another dispatcher took the row over; the caller must stop
    without touching the thread.
    """
    async with sessionmaker.begin() as session:
        return await start_wake_row(
            session,
            idempotency_key=claim.idempotency_key,
            owner=claim.owner,
            now=now,
            lease=WAKE_RUN_LEASE,
        )


async def settle_wake(
    sessionmaker: async_sessionmaker[AsyncSession],
    claim: WakeClaim,
    *,
    status: Literal["delivered", "skipped"],
    now: datetime,
    skip_reason: str | None = None,
) -> bool:
    """Close out a claim; False when it had already been taken over."""
    async with sessionmaker.begin() as session:
        settled = await settle_wake_row(
            session,
            idempotency_key=claim.idempotency_key,
            owner=claim.owner,
            status=status,
            now=now,
            skip_reason=skip_reason,
        )
    if not settled:
        log.warning("wake.settle_lost_claim", idempotency_key=str(claim.idempotency_key))
    return settled


async def release_wake(
    sessionmaker: async_sessionmaker[AsyncSession],
    claim: WakeClaim,
    *,
    retry_at: datetime | None,
) -> bool:
    """Hand a claim back when the caller KNOWS its turn did not run.

    The thread was busy, the session could not be bound: the row goes back to
    `pending` and the claim is refunded, so waiting never counts against
    `WAKE_MAX_ATTEMPTS`. `retry_at` is when a wake is offered again; None
    leaves `available_at` alone, which keeps a handoff (NULL) waiting for the
    next turn in its thread, as it did before the wake queue. False when the
    claim had already been lost.
    """
    async with sessionmaker.begin() as session:
        return await release_wake_row(
            session,
            idempotency_key=claim.idempotency_key,
            owner=claim.owner,
            retry_at=retry_at,
        )


def busy_retry_at(
    row: TaskContinuationRow, *, now: datetime, not_before: datetime | None = None
) -> datetime | None:
    """When a row released because its thread was busy is offered again.

    A wake is retried by the poller after `WAKE_RETRY_DELAY`, or at the busy
    session's own `retry_after` (`not_before`) if that is later. A handoff (no
    `available_at`) keeps waiting for the next turn in its thread.
    """
    if row.available_at is None:
        return None
    retry_at = now + WAKE_RETRY_DELAY
    return retry_at if not_before is None else max(retry_at, not_before)


async def cancel_wake(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    idempotency_key: uuid.UUID,
) -> bool:
    """Withdraw a pending wake; False once it is claimed, settled or not this tenant's."""
    async with sessionmaker.begin() as session:
        return await cancel_wake_row(session, tenant_id=tenant_id, idempotency_key=idempotency_key)


async def list_dispatchable_wakes(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    now: datetime,
) -> list[TaskContinuationRow]:
    """What a dispatcher in this thread may claim now, oldest first."""
    async with sessionmaker() as session:
        return await list_dispatchable_continuations(
            session,
            tenant_id=tenant_id,
            platform=platform,
            thread_id=thread_id,
            now=now,
            max_attempts=WAKE_MAX_ATTEMPTS,
        )


async def skip_thread_wakes(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    thread: WakeThread,
    reason: str,
    now: datetime,
) -> int:
    """Settle every due wake in a thread the adapter can no longer reach.

    For a thread that is gone or forbidden — not for a transient platform
    error, which should leave the rows for the next poll. Each row is claimed
    first, so a row another process is running is left alone.
    """
    skipped = 0
    for row in await list_dispatchable_wakes(
        sessionmaker,
        tenant_id=thread.tenant_id,
        platform=thread.platform,
        thread_id=thread.thread_id,
        now=now,
    ):
        claim = await claim_wake(sessionmaker, idempotency_key=row.idempotency_key, now=now)
        if claim is None:
            continue
        if await settle_wake(sessionmaker, claim, status="skipped", now=now, skip_reason=reason):
            skipped += 1
    return skipped


async def abandon_interrupted_wakes(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    now: datetime,
) -> list[TaskContinuationRow]:
    """Settle expired claims that must not run again (started, or out of attempts)."""
    async with sessionmaker.begin() as session:
        rows = await abandon_interrupted_wake_rows(
            session, platform=platform, now=now, max_attempts=WAKE_MAX_ATTEMPTS
        )
    for row in rows:
        log.warning(
            "wake.abandoned",
            idempotency_key=str(row.idempotency_key),
            thread_id=row.thread_id,
            reason=row.skip_reason,
        )
    return rows


async def list_due_wake_threads(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    now: datetime,
) -> list[WakeThread]:
    """Threads with a due wake or an expired unstarted claim, one entry each."""
    async with sessionmaker() as session:
        rows = await list_due_wake_rows(
            session,
            platform=platform,
            now=now,
            max_attempts=WAKE_MAX_ATTEMPTS,
            limit=_POLL_BATCH,
        )
    threads: dict[tuple[uuid.UUID, str], WakeThread] = {}
    for row in rows:
        threads.setdefault(
            (row.tenant_id, row.thread_id),
            WakeThread(
                tenant_id=row.tenant_id,
                platform=row.platform,
                parent_channel_id=row.parent_channel_id,
                thread_id=row.thread_id,
                requester_account_id=row.requester_account_id,
            ),
        )
    return list(threads.values())


async def poll_wakes_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    open_thread: WakeOpener,
    now: datetime,
) -> int:
    """One poll: settle what must not re-run, then open every thread with due work.

    Returns how many threads were opened. A thread whose opener returns False
    or raises is pushed back by `WAKE_UNAVAILABLE_DELAY`, so the next poll's
    batch reaches threads behind it; its rows are retried after that.
    """
    await abandon_interrupted_wakes(sessionmaker, platform=platform, now=now)
    threads = await list_due_wake_threads(sessionmaker, platform=platform, now=now)
    opened = 0
    for thread in threads:
        try:
            ok = await open_thread(thread)
        except Exception:
            log.exception("wake.open_thread_failed", thread_id=thread.thread_id)
            ok = False
        if ok:
            opened += 1
            continue
        async with sessionmaker.begin() as session:
            await defer_wake_rows(
                session,
                tenant_id=thread.tenant_id,
                platform=platform,
                thread_id=thread.thread_id,
                now=now,
                retry_at=now + WAKE_UNAVAILABLE_DELAY,
                max_attempts=WAKE_MAX_ATTEMPTS,
            )
    return opened


async def run_wake_poller(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    open_thread: WakeOpener,
    should_stop: Callable[[], bool],
    interval_s: float = WAKE_POLL_INTERVAL_S,
) -> None:
    """Poll forever (until `should_stop`), one `poll_wakes_once` per interval."""
    while not should_stop():
        try:
            await poll_wakes_once(
                sessionmaker, platform=platform, open_thread=open_thread, now=datetime.now(UTC)
            )
        except Exception:
            log.exception("wake.poll_failed", platform=platform)
        await asyncio.sleep(interval_s)
