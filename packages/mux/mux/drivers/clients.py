"""SDK clients a host keeps for the life of its process instead of building one per call.

A client owns an HTTP connection pool, so it belongs to one event loop. The host
asks for the client of a (key, ...) on the running loop and closes them all once,
at shutdown. The key is only ever held as `key_digest`.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable


def key_digest(api_key: str) -> str:
    """A truncated sha256 that names a key without revealing it; safe to log or store."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


class LoopClients[C]:
    """One client per `key` per event loop; `build` runs only on the first request."""

    def __init__(self, close: Callable[[C], Awaitable[None]]) -> None:
        self._close = close
        self._entries: dict[tuple[int, tuple[str, ...]], tuple[asyncio.AbstractEventLoop, C]] = {}

    def get(self, key: tuple[str, ...], build: Callable[[], C]) -> C:
        loop = asyncio.get_running_loop()
        # A loop that has ended took its connections with it; only the entry is left.
        self._entries = {k: v for k, v in self._entries.items() if not v[0].is_closed()}
        entry = self._entries.get((id(loop), key))
        if entry is None:
            entry = self._entries[id(loop), key] = (loop, build())
        return entry[1]

    async def aclose(self) -> None:
        """Close this loop's clients; any other loop's can only be closed on that loop."""
        loop = asyncio.get_running_loop()
        mine = [client for (running, client) in self._entries.values() if running is loop]
        self._entries = {}
        for client in mine:
            await self._close(client)
