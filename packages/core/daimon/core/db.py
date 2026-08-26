"""Async engine + session factory builders for daimon-core.

Pure dependency-injection helpers. There is NO module-level engine and NO
`get_session()` singleton — the CLI entrypoint constructs one at startup and
threads the `async_sessionmaker` into stores as an explicit parameter.
"""

from __future__ import annotations

import asyncpg.exceptions  # type: ignore[reportMissingTypeStubs]
from daimon.core.errors import DatabaseNotMigratedError
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DATABASE_NOT_MIGRATED_MESSAGE = (
    "database not migrated.\n"
    "  run: uv run alembic upgrade head\n"
    "  (with DAIMON_DATABASE_URL set to your target DB)"
)


async def ensure_database_migrated(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fail fast with an actionable hint when the core schema is absent.

    A connectivity-only ``SELECT 1`` succeeds against an empty database, so
    probe a table created by daimon's initial migration instead. Connection,
    authentication, and other operational failures deliberately propagate
    unchanged; only an absent relation is translated into the migration hint.
    """
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1 FROM tenants LIMIT 1"))
    except (ProgrammingError, asyncpg.exceptions.UndefinedTableError) as err:
        raise DatabaseNotMigratedError(DATABASE_NOT_MIGRATED_MESSAGE) from err


def build_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """Build an `AsyncEngine` for the given DSN.

    The caller owns lifecycle and must `await engine.dispose()` on shutdown.

    Adapters hold one engine for the whole process lifetime, so pooled
    connections outlive any single turn and go idle for hours between them. A
    managed Postgres reached over a private network path drops such connections
    without a FIN, and the pool cannot tell: it hands the dead socket back and
    the next query fails with `ConnectionDoesNotExistError`. `pool_pre_ping`
    validates on checkout and transparently substitutes a fresh connection;
    `pool_recycle` retires connections before they reach that idle window.

    Pre-ping only covers checkout, so a connection that dies mid-statement
    (a failover, say) still raises — that needs retry at the adapter boundary.
    """
    return create_async_engine(url, echo=echo, pool_pre_ping=True, pool_recycle=1800)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build an `async_sessionmaker` bound to `engine`.

    `expire_on_commit=False` so Pydantic mapping in stores can read attributes
    after commit without a reload.
    """
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
