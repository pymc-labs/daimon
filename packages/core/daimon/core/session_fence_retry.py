"""Bound each fence acquisition, releasing transactions and permits between attempts."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RETRY_MIN_S = 0.025
RETRY_MAX_S = 0.075
RETRY_CEILING_S = 1.0
_acquisitions: ContextVar[dict[str, float] | None] = ContextVar(
    "session_fence_acquisitions", default=None
)


class FenceUnavailable(Exception):
    """An acquisition failed; the owning transaction must roll back before retry."""

    def __init__(self, deadline: float) -> None:
        super().__init__()
        self.deadline = deadline


async def try_fence(db: AsyncSession, key: str) -> None:
    # turn.__init__ imports preparation and mutation; keep this import lazy.
    from daimon.core.turn.ceiling import TURN_CEILING_S

    acquisitions = _acquisitions.get()
    deadline = asyncio.get_running_loop().time() + TURN_CEILING_S
    if acquisitions is not None:
        deadline = acquisitions.setdefault(key, deadline)
    try:
        async with asyncio.timeout_at(deadline):
            acquired = (
                await db.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": key},
                )
            ).scalar_one()
    except TimeoutError as error:
        raise FenceUnavailable(deadline) from error
    if not acquired:
        raise FenceUnavailable(deadline)
    if acquisitions is not None:
        # Protected MA work and later acquisitions get no part of this budget.
        acquisitions.pop(key, None)


async def retry_fences[T](operation: Callable[[], Awaitable[T]]) -> T:
    """Retry acquisition-only failures after their contexts release resources.

    Each fence starts its own turn-ceiling deadline at its first try-lock,
    retaining it across failures until acquired. Gates queue without a timer.
    Callers must raise FenceUnavailable only before protected side effects.
    Failed attempts roll back and re-read/re-authorize on the next attempt.
    """
    token = _acquisitions.set({})
    delay = RETRY_MAX_S
    try:
        while True:
            try:
                return await operation()
            except FenceUnavailable as error:
                remaining = error.deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    from daimon.core.turn.errors import SessionBusyError

                    raise SessionBusyError(
                        pending_reasons=("session_unavailable",),
                        retry_after=datetime.now(UTC) + timedelta(seconds=1),
                    ) from None
                await asyncio.sleep(min(remaining, random.uniform(delay / 3, delay)))
                delay = min(RETRY_CEILING_S, delay * 2)
    finally:
        _acquisitions.reset(token)
