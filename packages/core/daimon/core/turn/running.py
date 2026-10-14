"""`RunningTurns` -- the process's registry of turns in progress.

`run_prepared_turn` registers the task it runs in for the whole call, so every
entry point that reaches it (a mention, a reply-to-continue button, a wizard
submit, a direct message) is counted without any bookkeeping of its own. Entry
points that accept work before they reach it (a wizard submit or a direct
message waiting for a turn slot, then preparing its session) also register for
the whole accepted operation, through its cleanup; entries nest per task.

A shutdown drain reads it to know what is still in progress. When its grace
runs out it calls `begin_shutdown` and cancels the tasks: cleanup code asks
`is_drain_cancel` to tell that cancellation from any other and to leave the
provider session recoverable (`docs/architecture.md`, shutdown drain).

One instance lives on `TurnDeps`, so it is shared by every turn the adapter
process runs. No I/O.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator


class RunningTurns:
    """Tasks inside a turn or an accepted turn operation, keyed by task."""

    def __init__(self) -> None:
        self._entries: dict[asyncio.Task[object], int] = {}
        self._shutting_down = False

    @contextlib.contextmanager
    def track(self) -> Iterator[None]:
        """Count the current task as in progress until the block exits."""
        task = asyncio.current_task()
        if task is None:  # a loop callback, not a task: nothing to count
            yield
            return
        self._entries[task] = self._entries.get(task, 0) + 1
        try:
            yield
        finally:
            remaining = self._entries.get(task, 0) - 1
            if remaining > 0:
                self._entries[task] = remaining
            else:
                self._entries.pop(task, None)

    def tasks(self) -> set[asyncio.Task[object]]:
        """The tasks still in progress. A finished task is never reported."""
        return {task for task in self._entries if not task.done()}

    def count(self) -> int:
        return len(self.tasks())

    def begin_shutdown(self) -> None:
        """Mark the cancellations that follow as a shutdown's. Irreversible."""
        self._shutting_down = True

    @property
    def shutting_down(self) -> bool:
        """Whether a shutdown drain has started cancelling turns in this process."""
        return self._shutting_down

    def is_drain_cancel(self) -> bool:
        """Whether the current task is unwinding from the shutdown drain's cancel."""
        task = asyncio.current_task()
        return self._shutting_down and task is not None and task.cancelling() > 0
