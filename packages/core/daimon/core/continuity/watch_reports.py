"""Dispatch watch reports: a fire that goes to its owner's DM or to a new thread.

A `watch_report` row names the thread its watch was made in, but never runs
there: the thread wake path cannot list it (`stores.task_continuations`), and
`watch_seed` refuses a reason that does not match the watch's delivery. This is
the only dispatcher that reads these rows. It keeps the wake queue's ladder:
claim, recheck the watch, commit the start fence, run, settle. The adapter runs
the turn and its output; every refusal it raises settles the row, never retries.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import anthropic as anthropic_pkg
import structlog
from daimon.core.anthropic_guard import (
    AnthropicDisabled,
    guard_selfhosted_channel,
    independent_wake_provider_context,
)
from daimon.core.continuity.wakes import (
    WAKE_MAX_ATTEMPTS,
    WAKE_POLL_INTERVAL_S,
    busy_retry_at,
    claim_wake,
    release_wake,
    settle_wake,
    start_wake,
)
from daimon.core.errors import DaimonError
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.task_continuations import list_due_watch_report_rows
from daimon.core.stores.watches import watch_seed
from daimon.core.turn.errors import (
    AdmissionDenied,
    SessionBusyError,
    SessionPreparationFailed,
    TurnNotStarted,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

__all__ = [
    "RunReport",
    "dispatch_watch_report",
    "poll_watch_reports_once",
    "run_watch_report_poller",
]

#: Runs one started report with its seed (the evidence-carrying user message); raises on refusal.
RunReport = Callable[[TaskContinuationRow, str], Awaitable[None]]
#: Hands one due row to the adapter, which dispatches it in its own task.
OpenReport = Callable[[TaskContinuationRow], Awaitable[None]]

_POLL_BATCH = 20


async def dispatch_watch_report(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: TaskContinuationRow,
    *,
    run_report: RunReport,
    dispatch_errors: tuple[type[Exception], ...] = (),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Claim, recheck, fence, run and settle one report; a lost race does nothing."""
    if row.reason != "watch_report":
        raise ValueError("Only watch reports run here")
    async with (
        independent_wake_provider_context(),
        guard_selfhosted_channel(
            sessionmaker,
            tenant_id=row.tenant_id,
            platform=row.platform,
            channel_id=row.parent_channel_id,
            thread_id=row.thread_id,
            caller_id=row.requester_external_user_id,
            force=row.target_ma_agent_id.startswith("sh_agent_"),
        ),
    ):
        claim = await claim_wake(sessionmaker, idempotency_key=row.idempotency_key, now=now())
        if claim is None:
            return
        async with sessionmaker() as db:
            seed = await watch_seed(db, row, now=now())
        if seed is None:
            # Paused, deleted, revoked or changed since it fired: nobody is waiting.
            await settle_wake(
                sessionmaker, claim, status="skipped", now=now(), skip_reason="skip_target_changed"
            )
            return
        if not await start_wake(sessionmaker, claim, now=now()):
            return
        skip_reason: str | None = None
        try:
            await run_report(row, seed)
        except AnthropicDisabled:
            log.warning("watch_report.provider_guard_denied", row_id=str(row.id))
            await release_wake(sessionmaker, claim, retry_at=busy_retry_at(row, now=now()))
            return
        except SessionBusyError as busy:
            # Nothing ran: the row goes back to pending with its claim refunded.
            retry_at = busy_retry_at(row, now=now(), not_before=busy.retry_after)
            await release_wake(sessionmaker, claim, retry_at=retry_at)
            return
        except TurnNotStarted as exc:
            if exc.capacity:
                await release_wake(sessionmaker, claim, retry_at=busy_retry_at(row, now=now()))
                return
            skip_reason = f"not_started:{exc.result}"
        except SessionPreparationFailed:
            skip_reason = "blocked_preparation_failed"
        except AdmissionDenied as exc:
            skip_reason = f"admission_denied:{exc.reason}"
        except (DaimonError, anthropic_pkg.APIError, *dispatch_errors) as exc:
            log.warning(
                "watch_report.dispatch_failed", row_id=str(row.id), error_type=type(exc).__name__
            )
            skip_reason = "dispatch_failed"
        await settle_wake(
            sessionmaker,
            claim,
            status="delivered" if skip_reason is None else "skipped",
            now=now(),
            skip_reason=skip_reason,
        )


async def poll_watch_reports_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    open_report: OpenReport,
    now: datetime,
) -> int:
    """Hand every due report to the adapter; how many were handed over."""
    async with sessionmaker() as session:
        rows = await list_due_watch_report_rows(
            session, platform=platform, now=now, max_attempts=WAKE_MAX_ATTEMPTS, limit=_POLL_BATCH
        )
    for row in rows:
        await open_report(row)
    return len(rows)


async def run_watch_report_poller(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    open_report: OpenReport,
    should_stop: Callable[[], bool],
    interval_s: float = WAKE_POLL_INTERVAL_S,
) -> None:
    """Poll forever (until `should_stop`), one `poll_watch_reports_once` per interval."""
    while not should_stop():
        try:
            await poll_watch_reports_once(
                sessionmaker, platform=platform, open_report=open_report, now=datetime.now(UTC)
            )
        except Exception:
            log.exception("watch_report.poll_failed", platform=platform)
        await asyncio.sleep(interval_s)
