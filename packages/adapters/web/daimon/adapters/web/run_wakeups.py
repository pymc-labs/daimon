"""One Postgres listener per web process, shared by every open run page.

A run page's stream polls the ledger and also wakes at once when the run notifies. The
wake used to cost each stream its own pooled connection for as long as the page stayed
open, so a few dozen open pages starved every other request. Now one connection listens
on each tenant's channel while a stream of that tenant is open, and fans notices out to
the streams in memory. A lost connection only slows the streams to their polling.
"""

from __future__ import annotations

import asyncio
import functools
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from daimon.core.workflows.runs import run_channel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

#: Open streams one process serves; each polls the ledger every couple of seconds.
MAX_STREAMS = 100
_LISTEN_ERRORS = (OSError, SQLAlchemyError, asyncpg.PostgresError, asyncpg.InterfaceError)


class RunWakeups:
    def __init__(self, engine: object, *, max_streams: int = MAX_STREAMS) -> None:
        self._engine = engine if isinstance(engine, AsyncEngine) else None
        self._max = max_streams
        self._lock = asyncio.Lock()
        # Waiting events by tenant, then run id.
        self._waiting = defaultdict[uuid.UUID, defaultdict[str, set[asyncio.Event]]](
            lambda: defaultdict(set)
        )
        self._conn: AsyncConnection | None = None
        self._raw: Any = None  # the asyncpg connection under `_conn`
        self._listening: dict[uuid.UUID, functools.partial[None]] = {}
        self.open = 0

    @property
    def full(self) -> bool:
        return self.open >= self._max

    @asynccontextmanager
    async def watch(self, tenant_id: uuid.UUID, run_id: uuid.UUID) -> AsyncIterator[asyncio.Event]:
        """Set when the run's ledger changes; stays quiet where LISTEN is not possible."""
        woke = asyncio.Event()
        key = str(run_id)
        self.open += 1
        try:
            async with self._lock:
                self._waiting[tenant_id][key].add(woke)
                await self._sync()
            yield woke
        finally:
            self.open -= 1
            async with self._lock:
                runs = self._waiting[tenant_id]
                runs[key].discard(woke)
                if not runs[key]:
                    del runs[key]
                if not runs:
                    del self._waiting[tenant_id]
                await self._sync()

    def _notice(
        self, tenant_id: uuid.UUID, _conn: object, _pid: int, _channel: str, run: str
    ) -> None:
        for woke in self._waiting.get(tenant_id, {}).get(run, ()):
            woke.set()

    async def _sync(self) -> None:
        """Listen on the channels of the tenants with open streams, and on no other."""
        try:
            if self._raw is not None and self._raw.is_closed():
                await self._drop()
            if self._waiting and self._raw is None and self._engine is not None:
                self._conn = await self._engine.connect()
                self._raw = (await self._conn.get_raw_connection()).driver_connection
            if self._raw is None:
                return
            for tenant_id in self._waiting.keys() - self._listening.keys():
                callback = functools.partial(self._notice, tenant_id)
                await self._raw.add_listener(run_channel(tenant_id), callback)
                self._listening[tenant_id] = callback
            for tenant_id in self._listening.keys() - self._waiting.keys():
                await self._raw.remove_listener(
                    run_channel(tenant_id), self._listening.pop(tenant_id)
                )
            if not self._waiting:
                await self._drop()
        except _LISTEN_ERRORS:
            await self._drop()

    async def _drop(self) -> None:
        """Give the connection back with its listeners removed, so the pool never reuses them."""
        conn, raw, self._conn, self._raw = self._conn, self._raw, None, None
        listening, self._listening = self._listening, {}
        try:
            if raw is not None and not raw.is_closed():
                for tenant_id, callback in listening.items():
                    await raw.remove_listener(run_channel(tenant_id), callback)
            if conn is not None:
                await conn.close()
        except _LISTEN_ERRORS:
            pass
