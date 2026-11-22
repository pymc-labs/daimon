"""Dispatch a due timer that fires in its owner's DM or in a plain channel.

An imported timer from a DM or a channel names a private scope (`dm:<uuid>`,
`ch:<uuid>`), not a thread, so the thread wake path cannot run it. It keeps the wake
queue's ladder: claim, commit the start fence, run, settle. The adapter runs the turn
and delivers its output; every refusal settles the row. Only a transient error before
the turn runs puts it back, and that retry is capped.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import anthropic as anthropic_pkg
import structlog
from daimon.core.anthropic_guard import AnthropicDisabled, independent_wake_provider_context
from daimon.core.bot_access import BOT_ACCESS_RETRY_DELAY, BotAccessUnavailable
from daimon.core.channel_backend import backend_refusal_causes
from daimon.core.continuity.messages import render_timer_seed
from daimon.core.continuity.wakes import (
    WAKE_MAX_ATTEMPTS,
    WAKE_RETRY_DELAY,
    busy_retry_at,
    claim_wake,
    is_transient_denial,
    release_wake,
    report_wake_failed,
    settle_wake,
    start_wake,
)
from daimon.core.errors import DaimonError
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.turn.errors import (
    AdmissionDenied,
    SessionBusyError,
    SessionPreparationFailed,
    TurnNotStarted,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

__all__ = ["DeliveryFailed", "DeliveryRetry", "RunScopeTimer", "dispatch_scope_timer"]

#: Runs one started timer with its seed and delivers the reply; raises on refusal.
RunScopeTimer = Callable[[TaskContinuationRow, str], Awaitable[None]]


class DeliveryFailed(DaimonError):
    """The turn ran but its reply could not be delivered; it settles, never re-runs."""


class DeliveryRetry(Exception):
    """A transient error before the turn ran: offer the timer again, up to a cap."""


async def dispatch_scope_timer(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: TaskContinuationRow,
    *,
    run_timer: RunScopeTimer,
    dispatch_errors: tuple[type[Exception], ...] = (),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Claim, fence, run and settle one scope timer; a lost race does nothing."""
    if row.reason != "timer":
        raise ValueError("Only timers run here")
    async with independent_wake_provider_context():
        claim = await claim_wake(sessionmaker, idempotency_key=row.idempotency_key, now=now())
        if claim is None:
            return
        if not await start_wake(sessionmaker, claim, now=now()):
            return
        skip_reason: str | None = None
        error_class: str | None = None
        try:
            # An imported timer carries no set time; the seed leaves it out.
            await run_timer(row, render_timer_seed(row.requested_work or ""))
        except AnthropicDisabled:
            log.warning("scope_timer.provider_guard_denied", row_id=str(row.id))
            report_wake_failed(
                sessionmaker,
                row.tenant_id,
                row.reason,
                "provider_guard_denied",
                thread_id=row.thread_id,
            )
            await release_wake(sessionmaker, claim, retry_at=busy_retry_at(row, now=now()))
            return
        except SessionBusyError as busy:
            # Nothing ran: the row goes back to pending with its claim refunded.
            retry_at = busy_retry_at(row, now=now(), not_before=busy.retry_after)
            await release_wake(sessionmaker, claim, retry_at=retry_at)
            return
        except BotAccessUnavailable:
            # The bot cannot reach the channel yet: nothing ran, so the claim is refunded and
            # the timer looks again later, as often as it takes for the bot to be let in.
            log.info("scope_timer.bot_access_unavailable", row_id=str(row.id))
            retry_at = busy_retry_at(row, now=now(), delay=BOT_ACCESS_RETRY_DELAY)
            await release_wake(sessionmaker, claim, retry_at=retry_at)
            return
        except DeliveryRetry:
            # The turn never ran. Each claim stays counted, so a failure that never
            # clears settles after WAKE_MAX_ATTEMPTS instead of re-running forever.
            if claim.attempts < WAKE_MAX_ATTEMPTS:
                await release_wake(
                    sessionmaker, claim, retry_at=now() + WAKE_RETRY_DELAY, refund=False
                )
                return
            log.warning("scope_timer.retries_exhausted", row_id=str(row.id))
            skip_reason = "dispatch_failed"
        except TurnNotStarted as exc:
            if exc.capacity:
                await release_wake(sessionmaker, claim, retry_at=busy_retry_at(row, now=now()))
                return
            skip_reason = f"not_started:{exc.result}"
        except SessionPreparationFailed:
            skip_reason = "blocked_preparation_failed"
        except AdmissionDenied as exc:
            if is_transient_denial(exc):
                await release_wake(sessionmaker, claim, retry_at=busy_retry_at(row, now=now()))
                return
            skip_reason = f"admission_denied:{exc.reason}"
        except (DaimonError, anthropic_pkg.APIError, *dispatch_errors) as exc:
            log.warning(
                "scope_timer.dispatch_failed",
                row_id=str(row.id),
                error_type=type(exc).__name__,
                causes=backend_refusal_causes(exc),
            )
            skip_reason, error_class = "dispatch_failed", type(exc).__name__
        await settle_wake(
            sessionmaker,
            claim,
            status="delivered" if skip_reason is None else "skipped",
            now=now(),
            skip_reason=skip_reason,
            row=row,
            error_class=error_class,
        )
