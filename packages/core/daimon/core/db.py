"""Async engine + session factory builders for daimon-core.

Pure dependency-injection helpers. There is NO module-level engine and NO
`get_session()` singleton — the CLI entrypoint constructs one at startup and
threads the `async_sessionmaker` into stores as an explicit parameter.
"""

from __future__ import annotations

import structlog
from daimon.core.config import load_crypto_settings
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


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


def build_session_factory(
    engine: AsyncEngine, *, crypto_keys: tuple[str, ...] | None = None
) -> async_sessionmaker[AsyncSession]:
    """Build an `async_sessionmaker` bound to `engine`.

    `expire_on_commit=False` so Pydantic mapping in stores can read attributes
    after commit without a reload.
    """
    if crypto_keys is None:
        crypto_keys = tuple(k.get_secret_value() for k in load_crypto_settings().keys)
    if not crypto_keys:
        structlog.get_logger(__name__).warning("agent_env.encryption_disabled")
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession,
        info={"crypto_keys": crypto_keys},
    )
