"""Dispatch queued task-continuations at Slack turn completion.

A handoff that carried work to continue queues a `task_continuations` row
(`daimon.core.continuity.continuation`) rather than running a second turn
inline; a wake (`daimon.core.continuity.wakes`) queues the same row with an
`available_at`. This module is the Slack-side caller of that contract: after
every turn completes, or when the wake poller opens the thread, it lists what
the thread may run now, claims each row under a lease (at-most-once across
restarts), decides whether it should still run, and either fences and runs the
receiving agent's first turn or posts the decision's skip copy.

`run_follow_up` is injected rather than built here so tests can assert
claim/skip/decide behaviour without running a second real turn — the actual
Slack turn (admit -> bind_session -> run_prepared_turn) is wired by the
caller in `app.py`, which already holds the web client, channel and thread
this dispatch runs against.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

import anthropic as anthropic_pkg
import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.slack.context import THREAD_PAGE_LIMIT
from daimon.core.continuity.continuation import (
    ContinuationRequest,
    ResponderChanged,
    decide_continuation,
)
from daimon.core.continuity.wakes import (
    WAKE_RETRY_DELAY,
    busy_retry_at,
    claim_wake,
    list_dispatchable_wakes,
    release_wake,
    settle_wake,
    start_wake,
)
from daimon.core.errors import DaimonError
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.turn.errors import (
    AdmissionDenied,
    SessionBusyError,
    SessionPreparationFailed,
)
from daimon.core.turn.protection import protection_state
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

__all__ = ["RunFollowUp", "dispatch_pending_continuations"]

#: Runs the receiving agent's first turn, seeded with the continuation's
#: `requested_work`. Raises on failure; the dispatcher settles the claimed row
#: as skipped (`blocked_preparation_failed` or `dispatch_failed`) so nothing is
#: left claimed by a process that will not deliver it.
RunFollowUp = Callable[[TaskContinuationRow, str], Awaitable[None]]


async def _latest_human_message_at(
    web_client: AsyncWebClient, *, channel: str, thread_ts: str, after: datetime
) -> datetime | None:
    """The newest human (non-bot) message timestamp after `after`, one page.

    Mirrors `context.build_delta_xml`'s `conversations.replies` call exactly
    (`oldest`, `inclusive=False`, `limit=THREAD_PAGE_LIMIT`): the dispatch
    decision must never depend on Slack history beyond what an ordinary turn
    would ever read.
    """
    response = await web_client.conversations_replies(  # pyright: ignore[reportUnknownMemberType]
        channel=channel,
        ts=thread_ts,
        oldest=f"{after.timestamp():.6f}",
        inclusive=False,
        limit=THREAD_PAGE_LIMIT,
    )
    messages = cast(list[dict[str, Any]], response["messages"])  # pyright: ignore[reportUnknownVariableType]
    latest: datetime | None = None
    for msg in messages:
        if "bot_id" in msg or not msg.get("user"):
            continue
        ts_value = cast(str | None, msg.get("ts"))
        if not ts_value:
            continue
        candidate = datetime.fromtimestamp(float(ts_value), tz=UTC)
        if latest is None or candidate > latest:
            latest = candidate
    return latest


async def dispatch_pending_continuations(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    web_client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    channel: str,
    thread_id: str,
    active_turn: bool,
    run_follow_up: RunFollowUp,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Claim and settle every continuation or due wake this thread may run now.

    Called from the Slack turn-completion path (once a turn's own answer has
    been posted and its marker cleared), so the common case is an empty list
    and this is one cheap store read. `active_turn` is the caller's own
    marker check for THIS thread, passed through unchanged to
    `decide_continuation` -- this function does not re-derive it.

    Each row is claimed before it is decided, so a decision to skip is still
    only ever made by the one process that will also settle it, and fenced
    (`start_wake`) before `run_follow_up`. A dispatch settles `delivered` only
    after `run_follow_up` returns without raising; a preparation failure
    settles `skipped`/`blocked_preparation_failed`, a thread whose previous
    turn is still running releases the row back to pending (claim refunded),
    and any other boundary error settles `skipped`/`dispatch_failed`. A process
    that dies holding a claim leaves it to the lease: retried if it never
    started, settled `interrupted` if it had.
    """

    async def _post(text: str, *, reason: str) -> None:
        # The access policy's may-post decision, asked right before each post:
        # a protected thread, or one whose protection can't be read, gets
        # nothing; the caller still settles the row.
        state = await protection_state(
            sessionmaker, tenant_id=tenant_id, channel_id=channel, thread_id=thread_id
        )
        if not state.may_post:
            log.info(
                "slack.continuation.notice_withheld",
                thread_id=thread_id,
                reason=reason,
                state=state.value,
            )
            return
        await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
            channel=channel, thread_ts=thread_id, text=text
        )

    rows = await list_dispatchable_wakes(
        sessionmaker, tenant_id=tenant_id, platform="slack", thread_id=thread_id, now=now()
    )
    for row in rows:
        claim = await claim_wake(sessionmaker, idempotency_key=row.idempotency_key, now=now())
        if claim is None:
            continue

        request = ContinuationRequest(
            tenant_id=row.tenant_id,
            platform="slack",
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
        latest_user_message_at = await _latest_human_message_at(
            web_client, channel=channel, thread_ts=thread_id, after=row.created_at
        )
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
            continue

        if decision.action == "dispatch":
            seed = decision.seed_user_message
            if seed is None:
                # decide_continuation never returns "dispatch" without one;
                # guards a caller-side contract break rather than a real path.
                log.error("slack.continuation.dispatch_missing_seed", row_id=str(row.id))
                await settle_wake(
                    sessionmaker, claim, status="skipped", now=now(), skip_reason="missing_seed"
                )
                continue
            if not await start_wake(sessionmaker, claim, now=now()):
                # Another dispatcher took the row over while this one decided.
                continue
            try:
                await run_follow_up(row, seed)
            except SessionPreparationFailed:
                log.warning("slack.continuation.dispatch_preparation_failed", row_id=str(row.id))
                await settle_wake(
                    sessionmaker,
                    claim,
                    status="skipped",
                    now=now(),
                    skip_reason="blocked_preparation_failed",
                )
                continue
            except ResponderChanged as exc:
                # A timer whose thread is answered by another agent now: say
                # so and stop; the turn never started.
                await _post(exc.message, reason="skip_target_changed")
                await settle_wake(
                    sessionmaker,
                    claim,
                    status="skipped",
                    now=now(),
                    skip_reason="skip_target_changed",
                )
                continue
            except AdmissionDenied as exc:
                # The balance or cap gate said no: the turn never started, and
                # a wake is held to the same policy as a mention. Not retried.
                log.info("slack.continuation.dispatch_admission_denied", row_id=str(row.id))
                await settle_wake(
                    sessionmaker,
                    claim,
                    status="skipped",
                    now=now(),
                    skip_reason=f"admission_denied:{exc.reason}",
                )
                continue
            except SessionBusyError as busy:
                # Same outcome as `skip_turn_running`, reached one step later:
                # a turn was still running in this thread when the follow-up
                # tried to bind, so the destination could not take the session
                # over. Nothing ran, so the SAME row goes back to pending with
                # its claim refunded: a handoff waits for the next turn in this
                # thread, as it always has; a wake is retried by the poller
                # shortly. Waiting never uses up the crash budget.
                log.warning("slack.continuation.dispatch_turn_running", row_id=str(row.id))
                await release_wake(
                    sessionmaker,
                    claim,
                    retry_at=busy_retry_at(row, now=now(), not_before=busy.retry_after),
                )
                continue
            except (DaimonError, anthropic_pkg.APIError, SlackApiError) as exc:
                log.warning(
                    "slack.continuation.dispatch_failed", row_id=str(row.id), error=str(exc)
                )
                await settle_wake(
                    sessionmaker, claim, status="skipped", now=now(), skip_reason="dispatch_failed"
                )
                continue
            await settle_wake(sessionmaker, claim, status="delivered", now=now())
            continue

        if decision.message is not None:
            await _post(decision.message, reason=decision.action)
        await settle_wake(
            sessionmaker, claim, status="skipped", now=now(), skip_reason=decision.action
        )
