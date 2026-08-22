"""SlackRuntime -- DI bundle for the Slack adapter process."""

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
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_resolver import ResolverCache, new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.turn.deps import TurnDeps
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class SlackRuntime:
    settings: Settings
    anthropic: AsyncAnthropic
    sessionmaker: async_sessionmaker[AsyncSession]
    billing_config: BillingConfig | None
    http_client: httpx.AsyncClient
    resolver_cache: ResolverCache
    turn_deps: TurnDeps
    # Bottom tier of the channel→tenant→deployment config cascade. Defaults to
    # empty (no deployment fallback) so existing construction sites stay valid;
    # build_runtime always parses the real defaults/config.yaml.
    deployment_default: DeploymentDefault = field(default_factory=DeploymentDefault)


def build_turn_deps(
    settings: Settings,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    deployment_default: DeploymentDefault,
    resolver_cache: ResolverCache,
    billing_config: BillingConfig | None,
) -> TurnDeps:
    """Derive the frozen `TurnDeps` bundle from `settings` (D-04/D-12).

    Built ONCE per runtime construction — mirrors Discord's
    `daimon.adapters.discord.runtime.build_turn_deps`. Duplicated here (not
    imported from Discord) because adapters must not import each other; this
    is the fernet-mount fix for SPEC Req 7(a) — `fernet` is now unconditional
    for every session Slack creates, instead of the historical gap where
    `create_session` was called without one.
    """
    crypto_keys = tuple(secret.get_secret_value() for secret in settings.crypto.keys)
    fernet = build_multifernet(crypto_keys) if crypto_keys else None
    public_url = str(settings.mcp.public_url) if settings.mcp.public_url is not None else None
    return TurnDeps(
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        defaults_root=settings.defaults_root,
        mcp=settings.mcp,
        billing_config=billing_config,
        markup=settings.billing.markup,
        fernet=fernet,
        github_fallback_pat=(
            settings.github.fallback_pat.get_secret_value()
            if settings.github.fallback_pat is not None
            else None
        ),
        github_app_id=settings.github.app_id,
        github_app_private_key=(
            settings.github.app_private_key.get_secret_value()
            if settings.github.app_private_key is not None
            else None
        ),
        public_url=public_url,
    )


@asynccontextmanager
async def build_runtime(settings: Settings) -> AsyncIterator[SlackRuntime]:
    engine = build_engine(str(settings.database.url))
    sm = build_session_factory(engine)
    deployment_default = parse_deployment_default(settings.defaults_root)
    # Shared, process-lifetime resolver cache (D-12) — Slack adopts Discord's
    # <=300s TTL semantics instead of building a fresh cache per turn.
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
            sm,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            billing_config=billing_config,
        )
        try:
            yield SlackRuntime(
                settings=settings,
                anthropic=anthropic,
                sessionmaker=sm,
                billing_config=billing_config,
                http_client=http_client,
                resolver_cache=resolver_cache,
                turn_deps=turn_deps,
                deployment_default=deployment_default,
            )
        finally:
            await engine.dispose()
