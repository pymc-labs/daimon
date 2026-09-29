"""Process-lifetime dependencies for the Teams adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from anthropic import AsyncAnthropic
from daimon.core.billing import BillingConfig, load_billing_config
from daimon.core.config import Settings
from daimon.core.constants import MA_MAX_RETRIES
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.loader import parse_deployment_default
from daimon.core.ma_resolver import ResolverCache, new_resolver_cache
from daimon.core.mcp_oauth import McpTokenProbe, probe_bearer_token
from daimon.core.scope import DeploymentDefault
from daimon.core.turn.deps import TurnDeps, build_turn_deps
from daimon.core.turn.outcomes import drain_outcomes
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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


@asynccontextmanager
async def build_runtime(settings: Settings) -> AsyncIterator[TeamsRuntime]:
    """Build the Teams runtime: DB, MA client and turn deps."""
    if settings.teams is None:
        raise ValueError("Teams runtime requires configured Teams settings")
    engine = build_engine(str(settings.database.url))
    sessionmaker = build_session_factory(
        engine, crypto_keys=tuple(k.get_secret_value() for k in settings.crypto.keys)
    )
    deployment_default = parse_deployment_default(settings.defaults_root)
    resolver_cache = new_resolver_cache()
    billing_config = load_billing_config()
    async with (
        AsyncAnthropic(
            api_key=settings.anthropic.api_key.get_secret_value(),
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
            )
        finally:
            await drain_outcomes()
            await engine.dispose()
