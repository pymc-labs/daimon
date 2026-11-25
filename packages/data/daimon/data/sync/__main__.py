"""`python -m daimon.data.sync`: the sync worker, a process of its own.

It does nothing unless the `data_sources` feature is on for the deployment, and waits for
the store to exist (`daimon data init`) before it schedules.
"""

from __future__ import annotations

import asyncio
import importlib
import signal
from contextlib import suppress

import psycopg
import structlog
from daimon.core.config import load_settings
from daimon.core.ports.gate import data_sources_on
from daimon.data import pool
from daimon.data.dsn import libpq_dsn
from daimon.data.schema import Schemas
from daimon.data.sync.scheduler import serve

log = structlog.get_logger()
RETRY = 30.0


async def _store_exists(url: str, schemas: Schemas) -> bool:
    try:
        async with await psycopg.AsyncConnection.connect(libpq_dsn(url), autocommit=True) as conn:
            cur = await conn.execute(
                b"select to_regclass(%s) is not null", (f"{schemas.data}.sync_runs",)
            )
            row = await cur.fetchone()
            return bool(row and row[0])
    except psycopg.Error:
        return False


async def main() -> None:
    importlib.import_module("daimon.data.sync.connectors")  # registers the kinds
    settings = load_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    if not data_sources_on(settings):
        log.info("sync.off", reason="the data_sources feature is off")
        await stop.wait()
        return
    schemas = Schemas.of(settings.data)
    while not await _store_exists(str(settings.database.url), schemas):
        log.info("sync.waiting", reason="the store is not set up")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), RETRY)
        if stop.is_set():
            return
    try:
        await serve(settings, stop)
    finally:
        await pool.close_pools()


if __name__ == "__main__":
    asyncio.run(main())
