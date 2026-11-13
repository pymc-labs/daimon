"""Wait out a task the turn has cancelled without eating the turn's own cancellation.

A turn cancels helper tasks (a stop waiter, the render loop, a stream reader) and
then awaits them so nothing outlives it. That await raises the helper's
`CancelledError`, which is expected and dropped. A cancellation aimed at the turn
itself (the ceiling's `asyncio.wait_for`, an adapter tearing the turn down) can land
on the same await and raises the same exception. Dropping that one lets the turn run
on past its ceiling, so `drained` re-raises it. `Task.cancelling()` tells the two
apart: it counts cancellation requests made to the running task that have not been
taken back with `uncancel()`, so a rise during the drain is the task's own.

The awaited helper has always finished when the await raises: cancelling a task
that awaits another task cancels that one too, and resumes only once it is done.
Code that cleans up more after a drain keeps those steps in a `finally`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator

__all__ = ["drained"]


@contextlib.contextmanager
def drained(*kinds: type[BaseException]) -> Iterator[None]:
    """Drop the exceptions in `kinds` (default: any) raised while awaiting a task
    this code cancelled, except a cancellation requested of the awaiting task."""
    caught: tuple[type[BaseException], ...] = kinds or (BaseException,)
    current = asyncio.current_task()
    before = current.cancelling() if current is not None else 0
    try:
        yield
    except caught as error:
        # Only a request made during the drain: a task already unwinding from its
        # own cancellation drops the helper's and finishes the rest of its cleanup.
        if (
            isinstance(error, asyncio.CancelledError)
            and current is not None
            and current.cancelling() > before
        ):
            raise
