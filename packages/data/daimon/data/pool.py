"""The store's connections: a psycopg pool per process, opened on first use."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from daimon.core.config import Settings
from daimon.data.dsn import libpq_dsn
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

type Pool = AsyncConnectionPool[AsyncConnection[DictRow]]

_pools: Final[dict[str, Pool]] = {}


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


async def close_pools() -> None:
    """Close every pool this process opened (shutdown and tests)."""
    pools = list(_pools.values())
    _pools.clear()
    for pool in pools:
        await pool.close()
