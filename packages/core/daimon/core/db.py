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


def build_engine(
    url: str,
    *,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_timeout: float = 30.0,
) -> AsyncEngine:
    """Build an `AsyncEngine` for the given DSN.

    The caller owns lifecycle and must `await engine.dispose()` on shutdown.
    Bounded pools require pool_size + max_overflow >= 4 at startup, reserving
    independent preparation and mutation slots and their nested-query headroom.

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
    if pool_size > 0 and max_overflow >= 0 and pool_size + max_overflow < 4:
        raise ValueError(
            "Session fences require pool_size + max_overflow >= 4 "
            "to reserve independent preparation and mutation capacity"
        )
    # hide_parameters keeps bound values (agent keys, tokens) out of the
    # SQL text that database errors carry into logs and error reports.
    return create_async_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        pool_recycle=1800,
        hide_parameters=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
    )


def build_session_factory(
    engine: AsyncEngine,
    *,
    crypto_keys: tuple[str, ...] | None = None,
    allow_plaintext: bool | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Build an `async_sessionmaker` bound to `engine`.

    `expire_on_commit=False` so Pydantic mapping in stores can read attributes
    after commit without a reload. Without crypto keys, agent key writes are
    refused unless `allow_plaintext` (default: the crypto settings) opts in.
    """
    if crypto_keys is None or allow_plaintext is None:
        crypto = load_crypto_settings()
        if crypto_keys is None:
            crypto_keys = tuple(k.get_secret_value() for k in crypto.keys)
        if allow_plaintext is None:
            allow_plaintext = crypto.allow_plaintext
    if not crypto_keys:
        log = structlog.get_logger(__name__)
        if allow_plaintext:
            log.warning("agent_env.encryption_disabled", allow_plaintext=True)
        else:
            log.error(
                "agent_env.encryption_keys_missing",
                hint="set DAIMON_CRYPTO__KEYS; agent key writes are refused until then",
            )
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession,
        info={"crypto_keys": crypto_keys, "crypto_allow_plaintext": allow_plaintext},
    )
