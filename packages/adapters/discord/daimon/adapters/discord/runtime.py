"""DiscordRuntime -- DI bundle for the Discord adapter process."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from daimon.core.billing import BillingConfig, load_billing_config
from daimon.core.config import Settings
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.loader import parse_deployment_default
from daimon.core.ma_resolver import ResolverCache, new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class DiscordRuntime:
    settings: Settings
    anthropic: AsyncAnthropic
    sessionmaker: async_sessionmaker[AsyncSession]
    notebook_rate_limiter: RateLimiter
    billing_config: BillingConfig | None
    deployment_default: DeploymentDefault
    resolver_cache: ResolverCache


@asynccontextmanager
async def build_runtime(settings: Settings) -> AsyncIterator[DiscordRuntime]:
    engine = build_engine(str(settings.database.url))
    sessionmaker = build_session_factory(engine)
    deployment_default = parse_deployment_default(settings.defaults_root)
    resolver_cache = new_resolver_cache()
    async with AsyncAnthropic(
        api_key=settings.anthropic.api_key.get_secret_value(),
        base_url=str(settings.anthropic.base_url),
    ) as anthropic:
        notebook_rate_limiter = RateLimiter(
            max_requests=settings.notebook.publish_rate_per_hour,
        )
        try:
            yield DiscordRuntime(
                settings=settings,
                anthropic=anthropic,
                sessionmaker=sessionmaker,
                notebook_rate_limiter=notebook_rate_limiter,
                billing_config=load_billing_config(),
                deployment_default=deployment_default,
                resolver_cache=resolver_cache,
            )
        finally:
            await engine.dispose()
