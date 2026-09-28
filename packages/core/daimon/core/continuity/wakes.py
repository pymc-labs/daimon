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
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

import structlog
from daimon.core.continuity.continuation import ContinuationRequest
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
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

__all__ = [
    "WAKE_CLAIM_LEASE",
    "WAKE_MAX_ATTEMPTS",
    "WAKE_POLL_INTERVAL_S",
    "WAKE_RETRY_DELAY",
    "WAKE_RUN_LEASE",
    "WAKE_UNAVAILABLE_DELAY",
    "WakeClaim",
    "WakeOpener",
    "WakeThread",
    "abandon_interrupted_wakes",
    "busy_retry_at",
    "cancel_wake",
    "claim_wake",
    "enqueue_wake",
    "list_dispatchable_wakes",
    "list_due_wake_threads",
    "poll_wakes_once",
    "release_wake",
    "run_wake_poller",
    "settle_wake",
    "skip_thread_wakes",
    "start_wake",
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


async def enqueue_wake(
    sessionmaker: async_sessionmaker[AsyncSession],
    request: ContinuationRequest,
    *,
    available_at: datetime,
) -> None:
    """Queue `request` to run in its thread no earlier than `available_at`."""
    async with sessionmaker.begin() as session:
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
