"""Async engine + session factory builders for daimon-core.

Pure dependency-injection helpers. There is NO module-level engine and NO
`get_session()` singleton — the CLI entrypoint constructs one at startup and
threads the `async_sessionmaker` into stores as an explicit parameter.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def build_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """Build an `AsyncEngine` for the given DSN.

    The caller owns lifecycle and must `await engine.dispose()` on shutdown.
    """
    return create_async_engine(url, echo=echo)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build an `async_sessionmaker` bound to `engine`.

    `expire_on_commit=False` so Pydantic mapping in stores can read attributes
    after commit without a reload.
    """
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
