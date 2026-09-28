"""Dispatch queued task-continuation turns and due wakes in one thread.

A handoff that carried work to continue queues a `task_continuations` row
(`daimon.core.stores.task_continuations`); a wake (`daimon.core.continuity.
wakes`) queues the same row with an `available_at`. Either is picked up here,
the next time ANY turn finishes in that thread or when the wake poller opens
the thread, always inside the thread's concurrency guard
(`DaimonBot._processing`) so no second mention can race the dispatch.

Every row is claimed under a lease and fenced (`start_wake`) right before its
turn runs, so a process that dies mid-dispatch leaves a row the poller can
either safely retry (never started) or settle as interrupted (started).

This module only decides and settles (via `daimon.core.continuity.continuation`,
the at-most-once contract) and drives Discord-specific reads (`thread.history`
for the supersede check, `thread.send` for skip copy). Running the actual
follow-up turn is injected as `run_follow_up` so tests can assert dispatch
happened without paying for a second real turn, and so this module never has
to know how `DaimonBot` builds a lifecycle.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import anthropic as _anthropic
import structlog
from anthropic import AsyncAnthropic
from daimon.core.continuity.continuation import (
    ContinuationDecision,
    ContinuationRequest,
    ResponderChanged,
    decide_continuation,
)
from daimon.core.continuity.wakes import (
    WAKE_RETRY_DELAY,
    WakeClaim,
    busy_retry_at,
    claim_wake,
    list_dispatchable_wakes,
    release_wake,
    settle_wake,
    start_wake,
)
from daimon.core.errors import DaimonError
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.turn.errors import (
    AdmissionDenied,
    SessionBusyError,
    SessionPreparationFailed,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger(__name__)

__all__ = ["RunFollowUp", "dispatch_pending_continuations"]

#: Runs the destination agent's first turn for one dispatched continuation.
#: Raises `SessionPreparationFailed` (or any `DaimonError`/`anthropic.APIError`/
#: `discord.HTTPException`) to signal the turn did not run; any other return
#: is treated as delivered.
RunFollowUp = Callable[[TaskContinuationRow, ContinuationDecision], Awaitable[None]]


async def _latest_human_message_at(thread: discord.Thread, *, after: datetime) -> datetime | None:
    """Newest human message timestamp in `thread` strictly after `after`, or None."""
    latest: datetime | None = None
    async for message in thread.history(limit=5):
        if message.author.bot:
            continue
        if message.created_at <= after:
            continue
        if latest is None or message.created_at > latest:
            latest = message.created_at
    return latest


async def _dispatch_one(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    row: TaskContinuationRow,
    claim: WakeClaim,
    thread: discord.Thread,
    run_follow_up: RunFollowUp,
    now: Callable[[], datetime],
) -> None:
    request = ContinuationRequest(
        tenant_id=row.tenant_id,
        platform="discord",
        parent_channel_id=row.parent_channel_id,
        thread_id=row.thread_id,
        requester_account_id=row.requester_account_id,
        requester_external_user_id=row.requester_external_user_id,
        target_ma_agent_id=row.target_ma_agent_id,
        target_name=row.target_name,
        requested_work=row.requested_work,
        reason=row.reason,
        idempotency_key=row.idempotency_key,
    )
    latest_user_message_at = await _latest_human_message_at(thread, after=row.created_at)
    async with sessionmaker() as session:
        live = await get_live_thread_session(
            session,
            tenant_id=row.tenant_id,
            platform="discord",
            thread_id=row.thread_id,
            account_id=row.requester_account_id,
        )
    active_turn = live is not None and live.active_turn_message_id is not None

    decision = await decide_continuation(
        sessionmaker,
        anthropic,
        request=request,
        now=now(),
        latest_user_message_at=latest_user_message_at,
        active_turn=active_turn,
    )

    if decision.action == "skip_turn_running" and row.available_at is not None:
        # A wake has no person waiting on a reply to be told anything; it
        # simply runs after the turn in progress.
        await release_wake(sessionmaker, claim, retry_at=now() + WAKE_RETRY_DELAY)
        return
    if decision.action != "dispatch":
        if decision.message is not None:
            await thread.send(decision.message)
        await settle_wake(
            sessionmaker, claim, status="skipped", now=now(), skip_reason=decision.action
        )
        return

    if not await start_wake(sessionmaker, claim, now=now()):
        # Another dispatcher took the row over while this one was deciding.
        return
    try:
        await run_follow_up(row, decision)
    except SessionPreparationFailed:
        log.warning("continuation.dispatch_preparation_failed", thread_id=row.thread_id)
        await settle_wake(
            sessionmaker,
            claim,
            status="skipped",
            now=now(),
            skip_reason="blocked_preparation_failed",
        )
        return
    except ResponderChanged as exc:
        # A timer whose thread is answered by another agent now: say so and
        # stop; the turn never started.
        await thread.send(exc.message)
        await settle_wake(
            sessionmaker, claim, status="skipped", now=now(), skip_reason="skip_target_changed"
        )
        return
    except AdmissionDenied as exc:
        # The balance or cap gate said no: the turn never started, and a wake
        # is held to the same policy as a mention. Not retried.
        log.info("continuation.dispatch_admission_denied", thread_id=row.thread_id)
        await settle_wake(
            sessionmaker,
            claim,
            status="skipped",
            now=now(),
            skip_reason=f"admission_denied:{exc.reason}",
        )
        return
    except SessionBusyError as busy:
        # Same outcome as `skip_turn_running`, reached one step later: a turn
        # was still running in this thread when the follow-up tried to bind, so
        # the destination could not take the session over. Nothing ran, so the
        # SAME row goes back to pending with its claim refunded: a handoff
        # waits for the next turn in this thread, as it always has; a wake is
        # retried by the poller shortly. Only a claim can run it, so
        # at-most-once holds, and waiting never uses up the crash budget.
        log.warning("continuation.dispatch_turn_running", thread_id=row.thread_id)
        await release_wake(
            sessionmaker, claim, retry_at=busy_retry_at(row, now=now(), not_before=busy.retry_after)
        )
        return
    except (DaimonError, _anthropic.APIError, discord.HTTPException) as exc:
        log.warning("continuation.dispatch_failed", thread_id=row.thread_id, error=str(exc))
        await settle_wake(
            sessionmaker, claim, status="skipped", now=now(), skip_reason="dispatch_failed"
        )
        return

    await settle_wake(sessionmaker, claim, status="delivered", now=now())


async def dispatch_pending_continuations(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    thread: discord.Thread,
    run_follow_up: RunFollowUp,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Claim and run every continuation or due wake `thread` may run now.

    Called inside the caller's concurrency guard for this thread. Every
    dispatchable row is claimed under a lease (`claim_wake` — an at-most-once
    conditional UPDATE, so a lost race here means another caller already owns
    it and this one does nothing), decided (`decide_continuation`), and either
    fenced and dispatched via `run_follow_up` or settled as skipped with the
    decision's own copy.

    `now` is a clock, read afresh for every claim, start, settle and release:
    a row dispatched after a long turn for the row before it must not be
    stamped with a time from before that turn (its lease would be born
    expired). Tests inject a fixed or stepping clock.
    """
    pending = await list_dispatchable_wakes(
        sessionmaker,
        tenant_id=tenant_id,
        platform="discord",
        thread_id=str(thread.id),
        now=now(),
    )
    for row in pending:
        claim = await claim_wake(sessionmaker, idempotency_key=row.idempotency_key, now=now())
        if claim is None:
            continue
        await _dispatch_one(
            sessionmaker,
            anthropic,
            row=row,
            claim=claim,
            thread=thread,
            run_follow_up=run_follow_up,
            now=now,
        )
