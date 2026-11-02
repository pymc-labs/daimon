"""Bound each fence acquisition, releasing transactions and permits between attempts."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

RETRY_MIN_S = 0.025
RETRY_MAX_S = 0.075
RETRY_CEILING_S = 1.0
_acquisitions: ContextVar[dict[str | int, float] | None] = ContextVar(
    "session_fence_acquisitions", default=None
)


#: Which acquisition failed: the advisory try-lock, a row taken NOWAIT (55P03), no
#: pooled connection in time, or the acquisition deadline itself.
FenceReason = Literal["advisory", "row_nowait", "pool_timeout", "deadline"]


class FenceUnavailable(Exception):
    """An acquisition failed; the owning transaction must roll back before retry."""

    def __init__(self, deadline: float, reason: FenceReason = "advisory") -> None:
        super().__init__()
        self.deadline = deadline
        self.reason: FenceReason = reason


async def try_fence(
    db: AsyncSession,
    key: str | int,
    *,
    shared: bool = False,
    timeout_s: float | None = None,
    schema_scoped: bool = False,
) -> None:
    # turn.__init__ imports preparation and mutation; keep this import lazy.
    from daimon.core.turn.ceiling import TURN_CEILING_S

    acquisitions = _acquisitions.get()
    deadline = asyncio.get_running_loop().time() + (
        TURN_CEILING_S if timeout_s is None else timeout_s
    )
    if acquisitions is not None:
        deadline = acquisitions.setdefault(key, deadline)
    try:
        async with asyncio.timeout_at(deadline):
            function = "pg_try_advisory_xact_lock_shared" if shared else "pg_try_advisory_xact_lock"
            if isinstance(key, int):
                argument = ":key"
            elif schema_scoped:
                # Advisory locks are database-wide; one schema's tenants never
                # contend with another's (test workers each own a schema).
                argument = "hashtextextended(current_schema() || ':' || :key, 0)"
            else:
                argument = "hashtextextended(:key, 0)"
            acquired = (
                await db.execute(text(f"SELECT {function}({argument})"), {"key": key})
            ).scalar_one()
    except TimeoutError as error:
        raise FenceUnavailable(deadline, "deadline") from error
    if not acquired:
        raise FenceUnavailable(deadline, "advisory")
    if acquisitions is not None:
        # Protected MA work and later acquisitions get no part of this budget.
        acquisitions.pop(key, None)


async def retry_fences[T](
    operation: Callable[[], Awaitable[T]],
    *,
    busy_error: Callable[[], Exception] | None = None,
    backoff: bool = True,
    label: str = "session_fence",
) -> T:
    """Retry acquisition-only failures after their contexts release resources.

    Each fence starts its own deadline at its first try-lock (the turn ceiling
    unless timeout_s is supplied), retaining it across failures until acquired.
    Gates queue without a timer. busy_error overrides the default session error.
    Callers must raise FenceUnavailable only before protected side effects.
    Failed attempts roll back and re-read/re-authorize on the next attempt.
    Set backoff=False for flat short polling when resolving a dispatched write;
    this avoids disadvantaging that waiter against newly arriving acquisitions.
    `label` (a fixed name, never data) is logged with every failure reason seen
    when the deadline runs out.
    """
    token = _acquisitions.set({})
    delay = RETRY_MAX_S
    seen: set[FenceReason] = set()
    try:
        while True:
            try:
                return await operation()
            except FenceUnavailable as error:
                seen.add(error.reason)
                remaining = error.deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    # The last attempt often fails on the deadline itself: every reason
                    # seen names what actually held the acquisition.
                    log.warning(
                        "session_fence.unavailable",
                        label=label,
                        reasons=sorted(seen),
                        last=error.reason,
                    )
                    if busy_error is not None:
                        raise busy_error() from None
                    from daimon.core.turn.errors import SessionBusyError

                    raise SessionBusyError(
                        pending_reasons=("session_unavailable",),
                        retry_after=datetime.now(UTC) + timedelta(seconds=1),
                    ) from None
                await asyncio.sleep(min(remaining, random.uniform(delay / 3, delay)))
                if backoff:
                    delay = min(RETRY_CEILING_S, delay * 2)
    finally:
        _acquisitions.reset(token)
