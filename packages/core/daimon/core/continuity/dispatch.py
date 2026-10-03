"""Run a thread's queued continuations and due wakes: claim, decide, then dispatch or settle.

The platform-neutral half of a continuation dispatcher, on the lease-based
wake queue (`daimon.core.continuity.wakes`). The adapter supplies the I/O as
callables: running the receiving agent's turn, posting a notice, and when a
person last spoke in the thread. A claim this process takes is settled or
released by it on every handled path; one it dies holding is left to the lease.
Notices go out only where the access policy lets the agent post.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import anthropic as anthropic_pkg
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
from daimon.core.stores.domain import ChatPlatform, TaskContinuationRow
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.turn.errors import AdmissionDenied, SessionBusyError, SessionPreparationFailed
from daimon.core.turn.protection import protection_state
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

__all__ = ["LatestMessageAt", "PostNotice", "RunFollowUp", "dispatch_pending_continuations"]

#: Runs the receiving agent's turn seeded with the requested work; raises on failure.
RunFollowUp = Callable[[TaskContinuationRow, str], Awaitable[None]]
#: When a person last posted in the row's thread, or None when unknown.
LatestMessageAt = Callable[[TaskContinuationRow], Awaitable[datetime | None]]
#: Posts a notice (a decision's skip copy, a changed responder) into the thread.
PostNotice = Callable[[str], Awaitable[None]]
DecisionContext = Callable[[TaskContinuationRow], Awaitable[tuple[datetime | None, bool]]]
Notice = Callable[[TaskContinuationRow, str, str], Awaitable[None]]
RunDecision = Callable[[TaskContinuationRow, ContinuationDecision], Awaitable[None]]


async def _settle(
    sessionmaker: async_sessionmaker[AsyncSession],
    claim: WakeClaim,
    skip_reason: str | None,
    now: Callable[[], datetime],
) -> None:
    await settle_wake(
        sessionmaker,
        claim,
        status="delivered" if skip_reason is None else "skipped",
        now=now(),
        skip_reason=skip_reason,
    )


async def dispatch_pending_continuations(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    platform: ChatPlatform,
    thread_id: str,
    run_follow_up: RunFollowUp | None = None,
    post_notice: PostNotice | None = None,
    latest_user_message_at: LatestMessageAt | None = None,
    dispatch_errors: tuple[type[Exception], ...] = (),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    decision_context: DecisionContext | None = None,
    notice: Notice | None = None,
    settle_before_notice: bool = True,
    run_decision: RunDecision | None = None,
    notify_missing_seed: bool = True,
) -> None:
    """Claim and settle every continuation or due wake this thread may run now, oldest first.

    Call while holding the adapter's per-thread guard. Each row is fenced
    (`start_wake`) before `run_follow_up`. A preparation failure settles
    `blocked_preparation_failed`; a changed timer responder posts its notice
    and settles `skip_target_changed`; an admission denial settles
    `admission_denied:<reason>`; a busy thread releases the row (claim
    refunded) for the next turn tail or, for a wake, a later poll; a
    `DaimonError`, `anthropic.APIError` or one of `dispatch_errors` settles
    `dispatch_failed`.

    Adapter ports preserve their history/active-turn read order and may-post
    checks. Discord runs the full decision; Slack and Teams run its seed.
    Teams settles before notices; Discord and Slack settle after them.
    The clock is read afresh at each queue transition.
    """

    if run_follow_up is None and run_decision is None:
        raise ValueError("A follow-up runner is required")

    if notice is None and post_notice is None:
        raise ValueError("A notice port is required")
    if decision_context is None and latest_user_message_at is None:
        raise ValueError("A history port is required")

    async def notify(row: TaskContinuationRow, text: str, *, reason: str) -> None:
        if notice is not None:
            await notice(row, text, reason)
            return
        # Posted outside any gated turn (wake poller, saved input), so may-post is
        # asked right before each post; the row settles either way.
        state = await protection_state(
            sessionmaker,
            tenant_id=row.tenant_id,
            channel_id=row.parent_channel_id,
            thread_id=row.thread_id,
        )
        if state.may_post:
            assert post_notice is not None
            await post_notice(text)
            return
        log.info(
            "continuation.notice_withheld", row_id=str(row.id), reason=reason, state=state.value
        )

    rows = await list_dispatchable_wakes(
        sessionmaker,
        tenant_id=tenant_id,
        platform=platform,
        thread_id=thread_id,
        now=now(),
    )
    for row in rows:
        claim = await claim_wake(sessionmaker, idempotency_key=row.idempotency_key, now=now())
        if claim is None:
            continue
        request = ContinuationRequest(
            tenant_id=row.tenant_id,
            platform=platform,
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
        if decision_context is None:
            async with sessionmaker() as session:
                live = await get_live_thread_session(
                    session,
                    tenant_id=row.tenant_id,
                    platform=platform,
                    thread_id=row.thread_id,
                    account_id=row.requester_account_id,
                )
            # Preserve core's clock read before the history callback.
            decision_now = now()
            assert latest_user_message_at is not None
            latest = await latest_user_message_at(row)
            active_turn = live is not None and live.active_turn_message_id is not None
        else:
            latest, active_turn = await decision_context(row)
            decision_now = now()
        decision = await decide_continuation(
            sessionmaker,
            anthropic,
            request=request,
            now=decision_now,
            latest_user_message_at=latest,
            active_turn=active_turn,
        )
        if decision.action == "skip_turn_running" and row.available_at is not None:
            # A wake has nobody waiting to be told; it runs after the turn in progress.
            await release_wake(sessionmaker, claim, retry_at=now() + WAKE_RETRY_DELAY)
            continue
        seed = decision.seed_user_message
        if decision.action != "dispatch" or (seed is None and run_decision is None):
            reason = decision.action if decision.action != "dispatch" else "missing_seed"
            if settle_before_notice:
                await _settle(sessionmaker, claim, reason, now)
            if decision.message is not None and (reason != "missing_seed" or notify_missing_seed):
                await notify(row, decision.message, reason=reason)
            if not settle_before_notice:
                await _settle(sessionmaker, claim, reason, now)
            continue
        if not await start_wake(sessionmaker, claim, now=now()):
            continue  # Another dispatcher took the row over while this one decided.
        try:
            if run_decision is not None:
                await run_decision(row, decision)
            else:
                assert run_follow_up is not None and seed is not None
                await run_follow_up(row, seed)
        except SessionPreparationFailed:
            await _settle(sessionmaker, claim, "blocked_preparation_failed", now)
        except ResponderChanged as exc:
            # A wake whose thread another agent answers now; the turn never started.
            if settle_before_notice:
                await _settle(sessionmaker, claim, "skip_target_changed", now)
            await notify(row, exc.message, reason="skip_target_changed")
            if not settle_before_notice:
                await _settle(sessionmaker, claim, "skip_target_changed", now)
        except AdmissionDenied as exc:
            # Held to the same gates as a mention, and not retried.
            await _settle(sessionmaker, claim, f"admission_denied:{exc.reason}", now)
        except SessionBusyError as busy:
            # Nothing ran: the same row goes back to pending with its claim refunded.
            retry_at = busy_retry_at(row, now=now(), not_before=busy.retry_after)
            await release_wake(sessionmaker, claim, retry_at=retry_at)
        except (DaimonError, anthropic_pkg.APIError, *dispatch_errors) as exc:
            log.warning("continuation.dispatch_failed", row_id=str(row.id), error=str(exc))
            await _settle(sessionmaker, claim, "dispatch_failed", now)
        else:
            await _settle(sessionmaker, claim, None, now)
