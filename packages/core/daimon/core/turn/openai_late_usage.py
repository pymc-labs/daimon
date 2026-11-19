"""Re-read an ended OpenAI turn whose usage was unknown, outside its turn.

A turn resource can report usage long after the turn ended (half an hour has been
seen). No turn waits for it: the host hands the turn here and returns. Each re-read
runs under the thread's lease and records a new revision through the turn's own
recorder, so the cost reaches the ledger and the daily cap like any other usage.
The re-reads live in this process: a restart before they finish leaves the turn
unknown, as it was before.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

import structlog
from daimon.core.channel_backend import backend_refusal_causes

log = structlog.get_logger(__name__)

#: Waits before each re-read, doubling: about an hour in all.
DELAYS_S = (60.0, 120.0, 240.0, 480.0, 960.0, 1920.0)

#: How long one late read of a turn resource may take, lease included.
READ_TIMEOUT_S = 20.0

#: Most turns waiting on a re-read at once, per process.
MAX_PENDING = 256

#: One re-read at `now`: True once the usage is known or can no longer be read.
type Reread = Callable[[datetime], Awaitable[bool]]


class LateUsage:
    """Background re-reads with bounded backoff, at most one per turn."""

    def __init__(
        self,
        *,
        delays_s: Sequence[float] = DELAYS_S,
        max_pending: int = MAX_PENDING,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._delays, self._sleep, self._clock = tuple(delays_s), sleep, clock
        self._max_pending = max_pending
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def defer(self, key: str, reread: Reread) -> None:
        """Schedule `reread` for `key` unless one is already scheduled; never waits.

        Past `max_pending` turns the new one is skipped and left unbilled.
        """
        if key in self._tasks:
            return
        if len(self._tasks) >= self._max_pending:
            log.warning("openai_late_usage.skipped", key=key, pending=len(self._tasks))
            return
        # A fresh context: nothing of the turn that scheduled it (its lease) carries over.
        task = asyncio.create_task(self._run(key, reread), context=contextvars.Context())
        self._tasks[key] = task
        task.add_done_callback(lambda _: self._tasks.pop(key, None))

    async def wait(self) -> None:
        """Until every scheduled re-read has finished."""
        while self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _run(self, key: str, reread: Reread) -> None:
        for attempt, delay in enumerate(self._delays, 1):
            await self._sleep(delay)
            try:
                if await reread(self._clock()):
                    return
            except Exception as error:  # a busy thread or failed read: retry later
                log.info(
                    "openai_late_usage.retry",
                    causes=backend_refusal_causes(error),
                    key=key,
                    attempt=attempt,
                    error_class=type(error).__name__,
                )
        log.warning("openai_late_usage.unknown", key=key, attempts=len(self._delays))


#: The process's re-reads, shared by every OpenAI runtime.
LATE_USAGE = LateUsage()
