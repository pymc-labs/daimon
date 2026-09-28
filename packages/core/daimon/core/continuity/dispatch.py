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


async def _settle(
    sessionmaker: async_sessionmaker[AsyncSession], claim: WakeClaim, skip_reason: str | None
) -> None:
    await settle_wake(
        sessionmaker,
        claim,
        status="delivered" if skip_reason is None else "skipped",
        now=datetime.now(UTC),
        skip_reason=skip_reason,
    )


async def _notify(
    sessionmaker: async_sessionmaker[AsyncSession],
    post_notice: PostNotice,
    row: TaskContinuationRow,
    text: str,
    *,
    reason: str,
) -> None:
    # Posted outside any gated turn (wake poller, saved input), so the may-post
    # decision is asked right before each post; the row settles either way.
    state = await protection_state(
        sessionmaker,
        tenant_id=row.tenant_id,
        channel_id=row.parent_channel_id,
        thread_id=row.thread_id,
    )
    if not state.may_post:
        log.info(
            "continuation.notice_withheld", row_id=str(row.id), reason=reason, state=state.value
        )
        return
    await post_notice(text)


async def dispatch_pending_continuations(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    platform: ChatPlatform,
    thread_id: str,
    run_follow_up: RunFollowUp,
    post_notice: PostNotice,
    latest_user_message_at: LatestMessageAt,
    dispatch_errors: tuple[type[Exception], ...] = (),
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
    """
    rows = await list_dispatchable_wakes(
        sessionmaker,
        tenant_id=tenant_id,
        platform=platform,
        thread_id=thread_id,
        now=datetime.now(UTC),
    )
    for row in rows:
        claim = await claim_wake(
            sessionmaker, idempotency_key=row.idempotency_key, now=datetime.now(UTC)
        )
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
        async with sessionmaker() as session:
            live = await get_live_thread_session(
                session,
                tenant_id=row.tenant_id,
                platform=platform,
                thread_id=row.thread_id,
                account_id=row.requester_account_id,
            )
        decision = await decide_continuation(
            sessionmaker,
            anthropic,
            request=request,
            now=datetime.now(UTC),
            latest_user_message_at=await latest_user_message_at(row),
            active_turn=live is not None and live.active_turn_message_id is not None,
        )
        if decision.action == "skip_turn_running" and row.available_at is not None:
            # A wake has nobody waiting to be told; it runs after the turn in progress.
            await release_wake(sessionmaker, claim, retry_at=datetime.now(UTC) + WAKE_RETRY_DELAY)
            continue
        seed = decision.seed_user_message
        if decision.action != "dispatch" or seed is None:
            # Settled before posting: a failed post must not leave the row claimed.
            reason = decision.action if decision.action != "dispatch" else "missing_seed"
            await _settle(sessionmaker, claim, reason)
            if decision.message is not None:
                await _notify(sessionmaker, post_notice, row, decision.message, reason=reason)
            continue
        if not await start_wake(sessionmaker, claim, now=datetime.now(UTC)):
            continue  # Another dispatcher took the row over while this one decided.
        try:
            await run_follow_up(row, seed)
        except SessionPreparationFailed:
            await _settle(sessionmaker, claim, "blocked_preparation_failed")
        except ResponderChanged as exc:
            # A timer whose thread another agent answers now; the turn never started.
            await _settle(sessionmaker, claim, "skip_target_changed")
            await _notify(sessionmaker, post_notice, row, exc.message, reason="skip_target_changed")
        except AdmissionDenied as exc:
            # Held to the same gates as a mention, and not retried.
            await _settle(sessionmaker, claim, f"admission_denied:{exc.reason}")
        except SessionBusyError as busy:
            # Nothing ran: the same row goes back to pending with its claim refunded.
            retry_at = busy_retry_at(row, now=datetime.now(UTC), not_before=busy.retry_after)
            await release_wake(sessionmaker, claim, retry_at=retry_at)
        except (DaimonError, anthropic_pkg.APIError, *dispatch_errors) as exc:
            log.warning("continuation.dispatch_failed", row_id=str(row.id), error=str(exc))
            await _settle(sessionmaker, claim, "dispatch_failed")
        else:
            await _settle(sessionmaker, claim, None)
