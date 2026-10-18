"""Process-lifetime dependencies for the Teams adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from secrets import randbits

import httpx
import structlog
from anthropic import AsyncAnthropic
from asyncpg import (  # pyright: ignore[reportMissingTypeStubs]
    InterfaceError,
    InternalClientError,
    PostgresError,
)
from daimon.core.billing import BillingConfig, load_billing_config
from daimon.core.channel_budget_notice import drain_budget_notices
from daimon.core.config import Settings
from daimon.core.constants import MA_MAX_RETRIES
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.loader import parse_deployment_default
from daimon.core.ma_resolver import ResolverCache, new_resolver_cache
from daimon.core.mcp_oauth import McpTokenProbe, probe_bearer_token
from daimon.core.native_admins import GroupMembers, GroupMembersCache
from daimon.core.scope import DeploymentDefault
from daimon.core.turn.deps import TurnDeps, build_turn_deps
from daimon.core.turn.outcomes import drain_outcomes
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

log = structlog.get_logger()


@dataclass(frozen=True)
class TeamsRuntime:
    """DI bundle for the Teams adapter process."""

    settings: Settings
    anthropic: AsyncAnthropic
    sessionmaker: async_sessionmaker[AsyncSession]
    billing_config: BillingConfig | None
    # Outbound HTTP outside the SDK. Redirects stay off: callers check every hop.
    http_client: httpx.AsyncClient
    resolver_cache: ResolverCache
    turn_deps: TurnDeps
    # Bottom tier of the channel→tenant→deployment config cascade.
    deployment_default: DeploymentDefault = field(default_factory=DeploymentDefault)
    # Pre-save check of a pasted MCP token; production wires `probe_bearer_token`.
    mcp_token_probe: McpTokenProbe | None = None
    # Owners of the admin team by Entra group id; None (no Graph) admits nobody
    # by team. Production wires `fetch_team_owner_ids`.
    team_owners: GroupMembers | None = None
    group_members: GroupMembersCache = field(default_factory=GroupMembersCache)
    # This process's message admissions; `hold_owner_key` makes it read as alive.
    owner_key: int = field(default_factory=lambda: randbits(63))


# How often the owner connection is checked; a relock retries from 1 s up to the cap.
OWNER_WATCH_INTERVAL_S = 15.0
_OWNER_RELOCK_MAX_DELAY_S = 120.0
_OWNER_PROBE_TIMEOUT_S = 5.0
# asyncpg failures can surface as raw socket errors, not SQLAlchemyError.
_OWNER_FAILURES = (
    SQLAlchemyError,
    InterfaceError,
    InternalClientError,
    PostgresError,
    OSError,
    TimeoutError,
)


async def _discard(connection: AsyncConnection) -> None:
    with suppress(*_OWNER_FAILURES):
        async with asyncio.timeout(_OWNER_PROBE_TIMEOUT_S):
            await connection.invalidate()
            await connection.close()


async def _lock_owner_key(engine: AsyncEngine, owner_key: int) -> AsyncConnection:
    """A new connection holding `owner_key`; a dead one's backend still holding it times out."""
    connection = await engine.connect()
    try:
        async with asyncio.timeout(_OWNER_PROBE_TIMEOUT_S):
            await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": owner_key})
            await connection.commit()
    except BaseException:
        await _discard(connection)
        raise
    return connection


@asynccontextmanager
async def hold_owner_key(
    engine: AsyncEngine, owner_key: int, *, watch_interval_s: float = OWNER_WATCH_INTERVAL_S
) -> AsyncIterator[None]:
    """Hold `owner_key` as a session advisory lock on its own connection, for life.

    Single process, so no heartbeat or fail-stop as Discord has: a watch checks
    the connection and takes the key again on a new one when it drops. Until
    then a later boot may reclaim this process's unhandled messages; the
    `finish_teams_messages` fence still keeps any of them from running twice.
    """
    connection: AsyncConnection | None = await _lock_owner_key(engine, owner_key)

    async def watch() -> None:
        nonlocal connection
        delay = watch_interval_s
        while True:
            await asyncio.sleep(delay)
            if connection is not None:
                try:
                    async with asyncio.timeout(_OWNER_PROBE_TIMEOUT_S):
                        await connection.execute(text("SELECT 1"))
                        await connection.commit()
                    continue
                except _OWNER_FAILURES as exc:
                    log.warning("teams.owner_key.lost", error_type=type(exc).__name__)
                    stale, connection, delay = connection, None, 1.0
                    await _discard(stale)
            try:
                connection = await _lock_owner_key(engine, owner_key)
            except _OWNER_FAILURES as exc:
                delay = min(delay * 2, _OWNER_RELOCK_MAX_DELAY_S)
                log.error(
                    "teams.owner_key.relock_failed",
                    error_type=type(exc).__name__,
                    retry_delay_s=delay,
                )
                continue
            log.info("teams.owner_key.relocked")
            delay = watch_interval_s

    watcher = asyncio.create_task(watch(), name="teams.owner-key-watch")
    try:
        yield
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
        if connection is not None:  # pyright: ignore[reportUnnecessaryComparison]  # the watch can drop it
            with suppress(*_OWNER_FAILURES):
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": owner_key}
                )
                await connection.commit()
            await _discard(connection)


@asynccontextmanager
async def build_runtime(settings: Settings) -> AsyncIterator[TeamsRuntime]:
    """Build the Teams runtime: DB, MA client and turn deps."""
    if settings.teams is None:
        raise ValueError("Teams runtime requires configured Teams settings")
    engine = build_engine(
        str(settings.database.url),
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout,
    )
    sessionmaker = build_session_factory(
        engine,
        crypto_keys=tuple(k.get_secret_value() for k in settings.crypto.keys),
        allow_plaintext=settings.crypto.allow_plaintext,
    )
    deployment_default = parse_deployment_default(settings.defaults_root)
    resolver_cache = new_resolver_cache()
    billing_config = load_billing_config()
    owner_key = randbits(63)
    async with (
        hold_owner_key(engine, owner_key),
        AsyncAnthropic(
            api_key=settings.anthropic.resolved_api_key(),
            base_url=str(settings.anthropic.base_url),
            max_retries=MA_MAX_RETRIES,
        ) as anthropic,
        httpx.AsyncClient(timeout=30.0) as http_client,
    ):
        turn_deps = build_turn_deps(
            settings,
            anthropic,
            sessionmaker,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            billing_config=billing_config,
        )
        try:
            yield TeamsRuntime(
                settings=settings,
                anthropic=anthropic,
                sessionmaker=sessionmaker,
                billing_config=billing_config,
                http_client=http_client,
                resolver_cache=resolver_cache,
                turn_deps=turn_deps,
                deployment_default=deployment_default,
                mcp_token_probe=probe_bearer_token,
                owner_key=owner_key,
            )
        finally:
            await drain_outcomes()
            await drain_budget_notices()
            await engine.dispose()
