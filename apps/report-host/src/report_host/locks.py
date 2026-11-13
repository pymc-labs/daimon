"""One lock per report slug, shared by every path that publishes or deletes a report."""

from __future__ import annotations

import asyncio


class SlugLocks:
    """Hands out the same ``asyncio.Lock`` for a slug, so a publish, an admin delete and the
    retention sweep never interleave on one report."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def __call__(self, slug: str) -> asyncio.Lock:
        lock = self._locks.get(slug)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[slug] = lock
        return lock
