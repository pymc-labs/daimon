"""Listens on Postgres for the MCP process's notices to this web process.

One long-lived connection per process. A lost connection is reopened after a pause;
a notice sent meanwhile is missed, and its card then shows only after a reload.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
import structlog
from daimon.core.stores.web_notices import (
    WEB_NOTICE_CHANNEL,
    WebCredentialNotice,
    parse_web_notice,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

log = structlog.get_logger(__name__)

#: Pause before reopening a lost connection.
RETRY_S = 5.0

Handle = Callable[[WebCredentialNotice], Awaitable[None]]
_CONNECTION_ERRORS = (OSError, SQLAlchemyError, asyncpg.PostgresError, asyncpg.InterfaceError)


class NoticeListener:
    def __init__(self, engine: AsyncEngine, handle: Handle, *, retry_s: float = RETRY_S) -> None:
        self._engine = engine
        self._handle = handle
        self._retry_s = retry_s
        self._tasks: set[asyncio.Task[None]] = set()
        self.listening = asyncio.Event()

    async def run(self) -> None:
        """Listen until cancelled."""
        while True:
            try:
                await self._listen_once()
            except _CONNECTION_ERRORS as err:
                log.warning("web.notices.connection_lost", err_type=type(err).__name__)
            self.listening.clear()
            await asyncio.sleep(self._retry_s)

    async def _listen_once(self) -> None:
        async with self._engine.connect() as conn:
            raw = (await conn.get_raw_connection()).driver_connection
            if raw is None:
                return
            lost = asyncio.Event()

            def on_lost(_conn: object) -> None:
                lost.set()

            raw.add_termination_listener(on_lost)
            await raw.add_listener(WEB_NOTICE_CHANNEL, self._on_payload)
            self.listening.set()
            try:
                await lost.wait()
            finally:
                if not raw.is_closed():
                    await raw.remove_listener(WEB_NOTICE_CHANNEL, self._on_payload)

    def _on_payload(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        notice = parse_web_notice(payload)
        if notice is None:
            return
        task = asyncio.create_task(self._handled(notice))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handled(self, notice: WebCredentialNotice) -> None:
        try:
            await self._handle(notice)
        except Exception:
            # Boundary: one bad notice must not stop the listener or reach the loop.
            log.exception("web.notices.handle_failed", kind=notice.kind)
