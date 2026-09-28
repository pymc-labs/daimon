"""Run a thread's queued continuations: claim each, decide, then dispatch or settle.

The platform-neutral half of a continuation dispatcher. The adapter supplies the
I/O as callables: running the receiving agent's turn, posting a skip notice, and
when a person last spoke in the thread. Every row this process claims is settled
by it on every path, so none is left claimed.
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
    claim_continuation,
    decide_continuation,
    record_continuation,
    settle_continuation,
)
from daimon.core.errors import DaimonError
from daimon.core.stores.domain import ChatPlatform, TaskContinuationRow
from daimon.core.stores.task_continuations import list_pending_continuations
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.turn.errors import SessionBusyError, SessionPreparationFailed
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

__all__ = ["LatestMessageAt", "PostNotice", "RunFollowUp", "dispatch_pending_continuations"]

#: Runs the receiving agent's turn seeded with the requested work; raises on failure.
RunFollowUp = Callable[[TaskContinuationRow, str], Awaitable[None]]
#: When a person last posted in the row's thread, or None when unknown.
LatestMessageAt = Callable[[TaskContinuationRow], Awaitable[datetime | None]]
#: Posts a decision's skip copy into the thread.
PostNotice = Callable[[str], Awaitable[None]]


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
    """Claim and settle every pending continuation for one thread, oldest first.

    Call while holding the adapter's per-thread guard. A preparation failure
    settles `blocked_preparation_failed`; a turn still running settles
    `turn_running` and re-queues the same work under a new key; a
    `DaimonError`, `anthropic.APIError` or one of `dispatch_errors` settles
    `dispatch_failed`.
    """
    async with sessionmaker() as session:
        rows = await list_pending_continuations(
            session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
        )
    for row in rows:
        if not await claim_continuation(
            sessionmaker, idempotency_key=row.idempotency_key, now=datetime.now(UTC)
        ):
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

        async def settle(skip_reason: str | None, key: uuid.UUID = row.idempotency_key) -> None:
            await settle_continuation(
                sessionmaker,
                idempotency_key=key,
                status="delivered" if skip_reason is None else "skipped",
                now=datetime.now(UTC),
                skip_reason=skip_reason,
            )

        seed = decision.seed_user_message
        if decision.action != "dispatch" or seed is None:
            # Settled before posting: a failed post must not leave the row claimed.
            await settle(decision.action if decision.action != "dispatch" else "missing_seed")
            if decision.message is not None:
                await post_notice(decision.message)
            continue
        try:
            await run_follow_up(row, seed)
        except SessionPreparationFailed:
            await settle("blocked_preparation_failed")
        except SessionBusyError:
            # The store has no "unclaim": settle this key and queue the same work
            # under a new one, so the next turn to finish here picks it up.
            await settle("turn_running")
            await record_continuation(
                sessionmaker, request.model_copy(update={"idempotency_key": uuid.uuid4()})
            )
        except (DaimonError, anthropic_pkg.APIError, *dispatch_errors) as exc:
            log.warning("continuation.dispatch_failed", row_id=str(row.id), error=str(exc))
            await settle("dispatch_failed")
        else:
            await settle(None)
