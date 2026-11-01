"""Process-lifetime dependencies for the web adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import structlog
from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.adapters.web.app_probe import AppStatusCache
from daimon.adapters.web.card_listener import NoticeListener
from daimon.adapters.web.directory import AgentDirectory
from daimon.adapters.web.single_process import hold_web_process_lock
from daimon.adapters.web.turns import WebTurns
from daimon.core.billing import BillingConfig, load_billing_config
from daimon.core.channel_budget_notice import drain_budget_notices
from daimon.core.config import Settings, WebSettings
from daimon.core.constants import MA_MAX_RETRIES
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.loader import (
    load_agent_templates,
    load_skill_descriptions,
    parse_deployment_default,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_resolver import ResolverCache, new_resolver_cache
from daimon.core.mcp_tool_listing import McpToolLister
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import AgentSpec
from daimon.core.turn.deps import build_turn_deps
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.web_oidc import OidcCache
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from stripe import StripeClient

log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class WebRuntime:
    """DI bundle for the web adapter process."""

    settings: Settings
    web: WebSettings
    sessionmaker: async_sessionmaker[AsyncSession]
    # Outbound HTTP to sign-in providers and embedded apps. Redirects stay off.
    http_client: httpx.AsyncClient
    fernet: MultiFernet
    oidc: OidcCache | None = None
    # Managed Agents, for the agents pages; None where the process reads no agents.
    anthropic: AsyncAnthropic | None = None
    # Agent lookups this process caches; an agent edit here clears its entries.
    resolver_cache: ResolverCache | None = None
    # Chat turns; None where the process runs no agents (sign-in only, and tests of it).
    turns: WebTurns | None = None
    # Card checkout for credit; None where Stripe is not set up.
    billing_config: BillingConfig | None = None
    stripe: StripeClient | None = None
    now: Callable[[], datetime] = field(default=_utcnow)
    # What the agent editor reads: servers' tool lists, the shipped skills' descriptions,
    # and the agent templates.
    mcp_tool_lister: McpToolLister | None = None
    deployment_default: DeploymentDefault = DeploymentDefault()
    skill_descriptions: Mapping[str, str] = field(default_factory=dict[str, str])
    agent_templates: Sequence[AgentSpec] = ()
    agents: AgentDirectory = field(default_factory=AgentDirectory)
    app_statuses: AppStatusCache = field(default_factory=AppStatusCache)


def _stripe_client(config: BillingConfig | None) -> StripeClient | None:
    """The Stripe client for card top-ups, when billing is configured and installed."""
    if config is None:
        return None
    try:
        from stripe import StripeClient  # the optional `billing` extra
    except ImportError:
        log.warning("web.checkout.stripe_missing")
        return None
    return StripeClient(config.secret_key.get_secret_value())


@asynccontextmanager
async def build_runtime(settings: Settings) -> AsyncIterator[WebRuntime]:
    """Open the database and HTTP client, and build the runtime around them."""
    if settings.web is None:
        raise ValueError("Web runtime requires configured web settings")
    web = settings.web
    keys = tuple(k.get_secret_value() for k in settings.crypto.keys)
    engine = build_engine(
        str(settings.database.url),
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout,
    )
    sessionmaker = build_session_factory(
        engine, crypto_keys=keys, allow_plaintext=settings.crypto.allow_plaintext
    )
    try:
        async with (
            hold_web_process_lock(engine),
            httpx.AsyncClient(timeout=15.0) as http_client,
            AsyncAnthropic(
                api_key=settings.anthropic.resolved_api_key(),
                base_url=str(settings.anthropic.base_url),
                max_retries=MA_MAX_RETRIES,
            ) as anthropic,
        ):
            billing_config = load_billing_config()
            deployment_default = parse_deployment_default(settings.defaults_root)
            # One cache, so an agent edited here is re-read by the next turn.
            resolver_cache = new_resolver_cache()
            deps = build_turn_deps(
                settings,
                anthropic,
                sessionmaker,
                deployment_default=deployment_default,
                resolver_cache=resolver_cache,
                billing_config=billing_config,
            )
            turns = WebTurns(settings, web, deps)
            listener = asyncio.create_task(
                NoticeListener(engine, turns.credentials.raise_card).run()
            )
            try:
                yield WebRuntime(
                    settings=settings,
                    web=web,
                    sessionmaker=sessionmaker,
                    http_client=http_client,
                    fernet=build_multifernet(keys),
                    oidc=OidcCache(http_client, web.oidc) if web.oidc is not None else None,
                    anthropic=anthropic,
                    resolver_cache=resolver_cache,
                    turns=turns,
                    billing_config=billing_config,
                    stripe=_stripe_client(billing_config),
                    mcp_tool_lister=McpToolLister(),
                    deployment_default=deployment_default,
                    skill_descriptions=load_skill_descriptions(settings.defaults_root / "skills"),
                    agent_templates=load_agent_templates(settings.defaults_root / "agents"),
                )
            finally:
                listener.cancel()
    finally:
        await drain_outcomes()
        await drain_budget_notices()
        await engine.dispose()
