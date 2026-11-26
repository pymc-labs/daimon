"""The store's connections: a psycopg pool per process, opened on first use."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import psycopg
from daimon.core.config import Settings
from daimon.data.dsn import libpq_dsn
from daimon.data.schema import Schemas
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

type Pool = AsyncConnectionPool[AsyncConnection[DictRow]]

_pools: Final[dict[str, Pool]] = {}
#: When each database's store was last found missing; a found store is not asked again.
_missing: Final[dict[str, float]] = {}
_installed: Final[set[str]] = set()
_RECHECK_S: Final = 60.0


async def _reset(conn: AsyncConnection[DictRow]) -> None:
    """Caller SQL may take session locks or settings: drop them before the connection is reused."""
    await conn.execute("select pg_advisory_unlock_all()")
    await conn.execute("reset all")


def _pool(settings: Settings) -> Pool:
    dsn = libpq_dsn(str(settings.database.url))
    if dsn not in _pools:
        _pools[dsn] = AsyncConnectionPool(
            dsn,
            min_size=0,
            max_size=settings.data.pool_size,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row},
            reset=_reset,
        )
    return _pools[dsn]


@asynccontextmanager
async def connection(settings: Settings) -> AsyncIterator[AsyncConnection[DictRow]]:
    """An autocommit connection; the pool opens, with no connection, on the first call."""
    pool = _pool(settings)
    await pool.open()
    async with pool.connection() as conn:
        yield conn


async def ready(settings: Settings) -> bool:
    """Whether the store is installed in this database. With data sources on by default, a
    database the release migration could not set up reads as the feature unavailable; it is
    asked again at most once a minute."""
    dsn = libpq_dsn(str(settings.database.url))
    if dsn in _installed:
        return True
    if time.monotonic() - _missing.get(dsn, -_RECHECK_S) < _RECHECK_S:
        return False
    try:
        async with connection(settings) as conn:
            cur = await conn.execute(
                "select to_regclass(%s) is not null as ok",
                (f"{Schemas.of(settings.data).data}.sources",),
            )
            row = await cur.fetchone()
    except psycopg.Error:
        row = None
    if row is not None and row["ok"]:
        _installed.add(dsn)
        return True
    _missing[dsn] = time.monotonic()
    return False


async def close_pools() -> None:
    """Close every pool this process opened (shutdown and tests)."""
    pools = list(_pools.values())
    _pools.clear()
    _installed.clear()
    _missing.clear()
    for pool in pools:
        await pool.close()
