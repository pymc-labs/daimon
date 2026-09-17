"""Process-lifetime dependencies for the Teams adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from anthropic import AsyncAnthropic
from daimon.adapters.teams.app import DirectCoreTurnDispatcher
from daimon.adapters.teams.identity import VerifiedTeamsTurnResolver
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
class TeamsRuntime:
    """DI bundle for the Teams adapter process."""

    settings: Settings
    anthropic: AsyncAnthropic
    sessionmaker: async_sessionmaker[AsyncSession]
    billing_config: BillingConfig | None
    resolver_cache: ResolverCache
    turn_deps: TurnDeps
    resolver: VerifiedTeamsTurnResolver
    dispatcher: DirectCoreTurnDispatcher
    # Bottom tier of the channel→tenant→deployment config cascade.
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
    """Derive the frozen `TurnDeps` bundle from `settings`.

    Mirrors the Slack adapter's ``build_turn_deps`` — duplicated here, not
    imported, because adapters must not import each other (import-linter
    independence contract).
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
async def build_runtime(settings: Settings) -> AsyncIterator[TeamsRuntime]:
    """Build the Teams runtime: DB, MA client, turn deps, resolver, dispatcher.

    The dispatcher runs turns in-process against core — the same
    background-task orchestration the Slack adapter uses.
    """
    teams_settings = settings.teams
    if teams_settings is None:
        raise ValueError("Teams runtime requires configured Teams settings")
    engine = build_engine(str(settings.database.url))
    sessionmaker = build_session_factory(engine)
    deployment_default = parse_deployment_default(settings.defaults_root)
    resolver_cache = new_resolver_cache()
    billing_config = load_billing_config()
    async with AsyncAnthropic(
        api_key=settings.anthropic.api_key.get_secret_value(),
        base_url=str(settings.anthropic.base_url),
        max_retries=MA_MAX_RETRIES,
    ) as anthropic:
        turn_deps = build_turn_deps(
            settings,
            anthropic,
            sessionmaker,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            billing_config=billing_config,
        )
        resolver = VerifiedTeamsTurnResolver(
            sessionmaker=sessionmaker,
            entra_tenant_id=teams_settings.tenant_id,
        )
        dispatcher = DirectCoreTurnDispatcher(
            turn_deps=turn_deps,
            sessionmaker=sessionmaker,
        )
        try:
            yield TeamsRuntime(
                settings=settings,
                anthropic=anthropic,
                sessionmaker=sessionmaker,
                billing_config=billing_config,
                resolver_cache=resolver_cache,
                turn_deps=turn_deps,
                resolver=resolver,
                dispatcher=dispatcher,
                deployment_default=deployment_default,
            )
        finally:
            await engine.dispose()
