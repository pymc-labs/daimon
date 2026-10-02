"""Bound fence acquisition, releasing transactions and permits between attempts."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

FENCE_WAIT_S = 5.0
RETRY_MIN_S = 0.025
RETRY_MAX_S = 0.075
_deadline: ContextVar[float | None] = ContextVar("session_fence_deadline", default=None)


class FenceUnavailable(Exception):
    """An acquisition failed; the owning transaction must roll back before retry."""


@asynccontextmanager
async def fence_acquisition() -> AsyncIterator[None]:
    """Apply the wait budget only to acquisition, never to protected MA work."""
    try:
        async with asyncio.timeout_at(_deadline.get()):
            yield
    except TimeoutError as error:
        raise FenceUnavailable from error


async def try_fence(db: AsyncSession, key: str) -> None:
    async with fence_acquisition():
        acquired = (
            await db.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": key},
            )
        ).scalar_one()
    if not acquired:
        raise FenceUnavailable


async def retry_fences[T](operation: Callable[[], Awaitable[T]]) -> T:
    """Retry an acquisition-only failure after its contexts have released resources.

    Callers must raise FenceUnavailable only before protected side effects.
    Failed attempts roll back and re-read/re-authorize on the next attempt.
    """
    deadline = asyncio.get_running_loop().time() + FENCE_WAIT_S
    token = _deadline.set(deadline)
    try:
        while True:
            try:
                return await operation()
            except FenceUnavailable:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    # turn.__init__ imports the preparation gate; keep this lazy.
                    from daimon.core.turn.errors import SessionBusyError

                    raise SessionBusyError(
                        pending_reasons=("session_unavailable",),
                        retry_after=datetime.now(UTC) + timedelta(seconds=1),
                    ) from None
                await asyncio.sleep(min(remaining, random.uniform(RETRY_MIN_S, RETRY_MAX_S)))
    finally:
        _deadline.reset(token)
