"""DiscordRuntime -- DI bundle for the Discord adapter process."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from secrets import randbits
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

import structlog
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
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
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.skills.rate_limit import SkillsRateLimitedTransport
from daimon.core.stores.discord_gateway_lease import GatewayLease, GatewayLeaseLost
from daimon.core.stores.domain import BackendAgent
from daimon.core.stores.worker_ownership import (
    OWNER_LOSS_GRACE_S,
    OWNER_PROBE_INTERVAL_S,
    OWNER_PROBE_TIMEOUT_S,
    OWNER_RETRY_BUDGET_S,
)
from daimon.core.turn.admission import AdmissionGrant
from daimon.core.turn.deps import TurnDeps, build_turn_deps
from daimon.core.turn.outcomes import drain_outcomes
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.adapters.discord.lifecycle import EditFn, SendFn
    from daimon.adapters.discord.posted_controls.fleet import DiscordFleetCard, ParentResolver
    from daimon.core.channel_budget import FleetCost
    from daimon.core.fleet_orchestration import FleetOrchestrator, HostFleetPorts
    from daimon.core.neutral_resources import WorkerPermissions
    from daimon.core.pricing import ModelRates, ProviderPrice
    from daimon.core.stores.agent_posts import AgentPostRow

    import discord


class FleetCardFactory(Protocol):
    def __call__(
        self,
        thread: discord.abc.Messageable,
        requester_id: int,
        send: SendFn,
        edit: EditFn,
        *,
        run_id: UUID | None = None,
        turn_card_intent_id: UUID | None = None,
        recorded_post: AgentPostRow | None = None,
        on_replacement: Callable[[discord.Message], Awaitable[None]] | None = None,
        wake_id: UUID | None = None,
    ) -> DiscordFleetCard | None: ...


log = structlog.get_logger()


class OwnershipExpired(Exception):
    """A replacement may already have reclaimed this owner's work."""


class GatewayLeaseExpired(OwnershipExpired, GatewayLeaseLost):
    """Another process may already have taken the Discord gateway."""


@asynccontextmanager
async def hold_worker_owner(
    engine: AsyncEngine,
    *,
    owner_key: int,
    ownership_lost: asyncio.Event,
    probe_interval_s: float = OWNER_PROBE_INTERVAL_S,
    gateway_lock_key: int | None = None,
) -> AsyncIterator[GatewayLease]:
    """Retry brief owner-connection loss; fail stop before reclaim becomes safe.

    Yields the gateway lease that shares this owner connection: a lease that
    cannot be re-confirmed after a reconnect fails stop like an expired owner.
    """
    # asyncpg failures can surface as raw socket errors, not SQLAlchemyError.
    failures = (
        SQLAlchemyError,
        InterfaceError,
        InternalClientError,
        PostgresError,
        OSError,
        TimeoutError,
    )

    connection: AsyncConnection | None = None
    # Serialises the heartbeat probe, reconnects and gateway lease queries on the one connection.
    connection_lock = asyncio.Lock()
    lease = GatewayLease(
        owner_key,
        connection=lambda: connection,
        lock=connection_lock,
        **({} if gateway_lock_key is None else {"lock_key": gateway_lock_key}),
    )

    async def acquire(*, reconnect: bool = False) -> AsyncConnection:
        candidate = await engine.connect()
        try:
            owned = await candidate.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": owner_key}
            )
            await candidate.commit()
            if not owned:
                raise SQLAlchemyError("worker owner key is still held")
            if reconnect and not await candidate.scalar(
                text("""SELECT EXISTS (
                         SELECT 1 FROM discord_worker_heartbeats
                         WHERE owner_key = :key AND seen_at >
                           clock_timestamp() - (:grace * interval '1 second'))"""),
                {"key": owner_key, "grace": OWNER_LOSS_GRACE_S},
            ):
                raise OwnershipExpired("owner heartbeat expired before reacquisition")
            if reconnect and lease.held and not await lease.confirm_after_reconnect(candidate):
                raise GatewayLeaseExpired("gateway lease moved on during owner reconnect")
            await candidate.execute(
                text("""INSERT INTO discord_worker_heartbeats (owner_key, seen_at, lease_aware)
                     VALUES (:key, clock_timestamp(), true)
                     ON CONFLICT (owner_key) DO UPDATE SET
                       seen_at = clock_timestamp(), lease_aware = true"""),
                {"key": owner_key},
            )
            await candidate.commit()
            if not reconnect:
                await candidate.execute(
                    text("""DELETE FROM discord_worker_heartbeats
                         WHERE owner_key != :key AND seen_at <
                           clock_timestamp() - (:age * interval '1 second')
                           AND NOT EXISTS (
                             SELECT 1 FROM pg_locks WHERE locktype = 'advisory'
                               AND granted AND objsubid = 1
                               AND database = (SELECT oid FROM pg_database
                                               WHERE datname = current_database())
                               AND classid = CAST(discord_worker_heartbeats.owner_key >> 32 AS oid)
                               AND objid = CAST(
                                 discord_worker_heartbeats.owner_key & 4294967295 AS oid)
                           )"""),
                    {"key": owner_key, "age": 3 * OWNER_LOSS_GRACE_S},
                )
                await candidate.commit()
            return candidate
        except BaseException:
            async with asyncio.timeout(OWNER_PROBE_TIMEOUT_S):
                await candidate.invalidate()
                await candidate.close()
            raise

    async with asyncio.timeout(OWNER_PROBE_TIMEOUT_S):
        connection = await acquire()

    async def discard() -> None:
        nonlocal connection
        stale, connection = connection, None
        if stale is not None:
            try:
                async with asyncio.timeout(OWNER_PROBE_TIMEOUT_S):
                    await stale.invalidate()
                    await stale.close()
            except failures as err:
                log.warning("discord.worker_owner_release_failed", error_type=type(err).__name__)

    async def guard() -> None:
        nonlocal connection
        while True:
            await asyncio.sleep(probe_interval_s)
            try:
                async with asyncio.timeout(OWNER_PROBE_TIMEOUT_S), connection_lock:
                    assert connection is not None
                    await connection.execute(
                        text("""UPDATE discord_worker_heartbeats
                             SET seen_at = clock_timestamp() WHERE owner_key = :key"""),
                        {"key": owner_key},
                    )
                    await connection.commit()
                continue
            except Exception as err:
                log.warning("discord.worker_owner_probe_failed", error_type=type(err).__name__)
            deadline = asyncio.get_running_loop().time() + OWNER_RETRY_BUDGET_S
            try:
                async with asyncio.timeout_at(deadline), connection_lock:
                    await discard()
                    while True:
                        try:
                            async with asyncio.timeout(OWNER_PROBE_TIMEOUT_S):
                                connection = await acquire(reconnect=True)
                            log.info("discord.worker_owner_recovered")
                            break
                        except OwnershipExpired:
                            raise
                        except failures as err:
                            log.warning(
                                "discord.worker_owner_retry_failed",
                                error_type=type(err).__name__,
                            )
                            await asyncio.sleep(1)
            except (TimeoutError, OwnershipExpired) as err:
                log.error("discord.worker_owner_lost", error_type=type(err).__name__)
                ownership_lost.set()
                await discard()
                return

    guard_task = asyncio.create_task(guard(), name="discord.worker_owner_guard")
    try:
        yield lease
    finally:
        guard_task.cancel()
        with suppress(asyncio.CancelledError):
            await guard_task
        if connection is not None:  # pyright: ignore[reportUnnecessaryComparison]  # guard can discard it
            try:
                # The lock: a lease call still on this connection must finish first.
                async with asyncio.timeout(OWNER_PROBE_TIMEOUT_S), connection_lock:
                    await connection.execute(
                        text("DELETE FROM discord_worker_heartbeats WHERE owner_key = :key"),
                        {"key": owner_key},
                    )
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": owner_key}
                    )
                    await connection.commit()
                    await connection.close()
                connection = None
            except failures:
                await discard()


@dataclass(frozen=True)
class DiscordRuntime:
    settings: Settings
    anthropic: AsyncAnthropic
    sessionmaker: async_sessionmaker[AsyncSession]
    notebook_rate_limiter: RateLimiter
    billing_config: BillingConfig | None
    deployment_default: DeploymentDefault
    resolver_cache: ResolverCache
    turn_deps: TurnDeps
    # Each production process holds this advisory key on a dedicated connection.
    owner_key: int = field(default_factory=lambda: randbits(63))
    ownership_lost: asyncio.Event = field(default_factory=asyncio.Event)
    # The MCP token form's live check. None means "do not probe" — the seam
    # tests use so a form submit never leaves the process; production wires
    # `daimon.core.mcp_oauth.probe_bearer_token`.
    mcp_token_probe: McpTokenProbe | None = None
    # Decides which Discord process may hold the gateway during a deploy overlap.
    # None means "no lease to take" (tests, tools that never connect).
    gateway_lease: GatewayLease | None = None
    # Host-built current-root service binding; ordinary construction discovers nothing.
    fleet_card_factory: FleetCardFactory | None = None
    # An ended run's cost by (tenant, run), for its card after the root turn is over.
    fleet_run_cost: Callable[[UUID, UUID], Awaitable[FleetCost | None]] | None = None


@asynccontextmanager
async def build_runtime(
    settings: Settings,
    *,
    fleet_service: FleetOrchestrator | None = None,
    fleet_parent_loader: ParentResolver | None = None,
    fleet_prices: Mapping[tuple[str, str], ModelRates | ProviderPrice] | None = None,
    fleet_root_grants: Callable[[AdmissionGrant, BackendAgent], Awaitable[WorkerPermissions]]
    | None = None,
) -> AsyncIterator[DiscordRuntime]:
    fleet_factory: FleetCardFactory | None = None
    owned_fleet_ports: HostFleetPorts | None = None
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
    async with AsyncAnthropic(
        api_key=settings.anthropic.resolved_api_key(),
        base_url=str(settings.anthropic.base_url),
        max_retries=MA_MAX_RETRIES,
        http_client=DefaultAsyncHttpxClient(
            transport=SkillsRateLimitedTransport(settings.anthropic.skills_requests_per_minute)
        ),
    ) as anthropic:
        notebook_rate_limiter = RateLimiter(
            max_requests=settings.notebook.publish_rate_per_hour,
        )
        turn_deps = build_turn_deps(
            settings,
            anthropic,
            sessionmaker,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            billing_config=billing_config,
            fleet_root_grants=fleet_root_grants if settings.fleet.enabled else None,
        )
        if settings.fleet.enabled:
            from daimon.adapters.discord.posted_controls.fleet import DiscordFleetCardFactory
            from daimon.core.fleet_orchestration import FleetOrchestrator, HostFleetPorts
            from daimon.core.stores.fleet import WorkerPool
            from daimon.core.stores.mux_state import PostgresStateStore

            if turn_deps.state_store is None:
                turn_deps = replace(turn_deps, state_store=PostgresStateStore(sessionmaker))
            if fleet_service is None:
                owned_fleet_ports = HostFleetPorts(
                    sessionmaker,
                    prices=fleet_prices,
                    markup=turn_deps.markup,
                    deps=turn_deps,
                    pool=WorkerPool(
                        max_concurrent_workers=settings.fleet.max_concurrent_workers,
                        max_concurrent_workers_per_tenant=(
                            settings.fleet.max_concurrent_workers_per_tenant
                        ),
                    ),
                )
                fleet_service = FleetOrchestrator(owned_fleet_ports)
            fleet_factory = (
                DiscordFleetCardFactory.from_runtime(fleet_service, turn_deps)
                if fleet_parent_loader is None
                else DiscordFleetCardFactory(fleet_service, fleet_parent_loader)
            )
        owner_key = randbits(63)
        ownership_lost = asyncio.Event()
        # Keep ownership outside the turn pool's preparation/mutation budget.
        # This pool opens one connection; its unused overflow satisfies the
        # shared builder's minimum capacity contract.
        owner_engine = build_engine(str(settings.database.url), pool_size=1, max_overflow=3)
        try:
            async with hold_worker_owner(
                owner_engine, owner_key=owner_key, ownership_lost=ownership_lost
            ) as gateway_lease:
                yield DiscordRuntime(
                    owner_key=owner_key,
                    ownership_lost=ownership_lost,
                    settings=settings,
                    anthropic=anthropic,
                    sessionmaker=sessionmaker,
                    notebook_rate_limiter=notebook_rate_limiter,
                    billing_config=billing_config,
                    deployment_default=deployment_default,
                    resolver_cache=resolver_cache,
                    turn_deps=turn_deps,
                    mcp_token_probe=probe_bearer_token,
                    gateway_lease=gateway_lease,
                    fleet_card_factory=fleet_factory,
                    fleet_run_cost=None if fleet_service is None else fleet_service.ended_run_cost,
                )
        finally:
            try:
                if owned_fleet_ports is not None:
                    turn_deps.running_turns.begin_shutdown()
                    await owned_fleet_ports.close()
            finally:
                await drain_outcomes()
                await drain_budget_notices()
                await owner_engine.dispose()
                await engine.dispose()
