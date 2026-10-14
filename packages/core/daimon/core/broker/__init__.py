"""Token broker dispatch.

The broker is a plain async function plus a stateless registry of provider
instances. No module-level mutable state crosses request boundaries; provider
classes hold no per-instance config (settings + sessionmaker come in via
``mint_token`` kwargs — prefer pure functions and dependency injection).

Audit logs deliberately carry only metadata — service, account UUID, agent
UUID, outcome — never the token plaintext.
"""

from __future__ import annotations

import uuid

import structlog
from daimon.core.broker.errors import NoBindingError, ProviderConfigError
from daimon.core.broker.providers import TokenProvider
from daimon.core.broker.providers.gcloud import GcloudTokenProvider
from daimon.core.broker.providers.github import GitHubTokenProvider
from daimon.core.config import Settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger()

# Stateless protocol implementers — module-level dict is acceptable
# (no I/O, no settings cached); provider configuration is injected per call.
_REGISTRY: dict[str, TokenProvider] = {
    GitHubTokenProvider.service: GitHubTokenProvider(),
    GcloudTokenProvider.service: GcloudTokenProvider(),
}


async def dispatch_mint_token(
    *,
    service: str,
    account_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    allow_service_default: bool = False,
) -> str:
    """Mint a token for ``service``. Audit-logs metadata only — never the token.

    ``allow_service_default`` passes straight through to the provider — the
    broker itself makes no opt-in decision. Each consumer of this function
    decides whether its use of the token is transient (safe to opt in) or
    durable (must stay at the default so it never inherits the shared
    operator secret).
    """
    provider = _REGISTRY.get(service)
    if provider is None:
        raise ProviderConfigError(f"unknown service: {service!r}")
    try:
        token = await provider.mint_token(
            account_id=account_id,
            agent_id=agent_id,
            sessionmaker=sessionmaker,
            settings=settings,
            allow_service_default=allow_service_default,
        )
    except NoBindingError:
        logger.warning(
            "broker.mint outcome=no_binding service=%s account=%s agent=%s",
            service,
            account_id,
            agent_id,
        )
        raise
    except ProviderConfigError:
        logger.warning(
            "broker.mint outcome=provider_config_error service=%s account=%s",
            service,
            account_id,
        )
        raise
    logger.info(
        "broker.mint outcome=success service=%s account=%s agent=%s",
        service,
        account_id,
        agent_id,
    )
    return token


__all__ = [
    "NoBindingError",
    "ProviderConfigError",
    "dispatch_mint_token",
]
