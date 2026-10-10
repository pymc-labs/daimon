"""DiscordRuntime -- DI bundle for the Discord adapter process."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from secrets import randbits

import structlog
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from asyncpg import InternalClientError
from daimon.core.billing import BillingConfig, load_billing_config
from daimon.core.channel_admins import GroupMembersCache
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
from daimon.core.turn.deps import TurnDeps, build_turn_deps
from daimon.core.turn.outcomes import drain_outcomes
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

log = structlog.get_logger()


@asynccontextmanager
async def hold_worker_owner(
    engine: AsyncEngine, *, owner_key: int, probe_interval_s: float = 5
) -> AsyncIterator[None]:
    """Keep ownership on one connection; exit if that connection loses its lock."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": owner_key})
        await connection.commit()
        parent = asyncio.current_task()
        assert parent is not None
        ownership_lost = False

        async def guard() -> None:
            nonlocal ownership_lost
            try:
                while True:
                    await asyncio.sleep(probe_interval_s)
                    async with asyncio.timeout(5):
                        await connection.execute(text("SELECT 1"))
                        await connection.commit()
            except (SQLAlchemyError, InternalClientError, TimeoutError) as err:
                ownership_lost = True
                log.error("discord.worker_owner_lost", error_type=type(err).__name__)
                parent.cancel()

        guard_task = asyncio.create_task(guard(), name="discord.worker_owner_guard")
        try:
            yield
        finally:
            guard_task.cancel()
            with suppress(asyncio.CancelledError):
                await guard_task
            try:
                if ownership_lost:
                    await connection.invalidate()
                else:
                    async with asyncio.timeout(5):
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(:key)"), {"key": owner_key}
                        )
                        await connection.commit()
            except (SQLAlchemyError, InternalClientError, TimeoutError) as err:
                # Lost ownership already cancels the adapter. Cleanup must
                # preserve that cancellation, even if the backend is gone.
                log.warning("discord.worker_owner_release_failed", error_type=type(err).__name__)


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
    # The MCP token form's live check. None means "do not probe" — the seam
    # tests use so a form submit never leaves the process; production wires
    # `daimon.core.mcp_oauth.probe_bearer_token`.
    mcp_token_probe: McpTokenProbe | None = None
    # Members' current roles, re-read outside their turns (`channel_admin_roles`).
    group_members: GroupMembersCache = field(default_factory=GroupMembersCache)


@asynccontextmanager
async def build_runtime(settings: Settings) -> AsyncIterator[DiscordRuntime]:
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
        api_key=settings.anthropic.api_key.get_secret_value(),
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
        )
        owner_key = randbits(63)
        # Keep ownership outside the turn pool's preparation/mutation budget.
        # This pool opens one connection; its unused overflow satisfies the
        # shared builder's minimum capacity contract.
        owner_engine = build_engine(str(settings.database.url), pool_size=1, max_overflow=3)
        try:
            async with hold_worker_owner(owner_engine, owner_key=owner_key):
                yield DiscordRuntime(
                    owner_key=owner_key,
                    settings=settings,
                    anthropic=anthropic,
                    sessionmaker=sessionmaker,
                    notebook_rate_limiter=notebook_rate_limiter,
                    billing_config=billing_config,
                    deployment_default=deployment_default,
                    resolver_cache=resolver_cache,
                    turn_deps=turn_deps,
                    mcp_token_probe=probe_bearer_token,
                )
        finally:
            await drain_outcomes()
            await drain_budget_notices()
            await owner_engine.dispose()
            await engine.dispose()
