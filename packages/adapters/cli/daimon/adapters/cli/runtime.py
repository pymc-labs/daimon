"""CliRuntime — DI bundle threaded through every CLI command."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from daimon.core.config import Settings
from daimon.core.constants import MA_MAX_RETRIES
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.loader import parse_deployment_default
from daimon.core.errors import DaimonError
from daimon.core.ma_resolver import ResolverCache, new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.skills.rate_limit import SkillsRateLimitedTransport
from daimon.core.turn.outcomes import drain_outcomes
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    # Typing only: the default CLI path never imports the neutral resource host.
    from daimon.core.neutral_resources import NeutralResourceHost

# A command that creates an agent waits this long for its face: a background
# render dies with the process. The render takes about a second.
FACE_WAIT_S = 10.0


class ResourceHostFactory(Protocol):
    """An operator-supplied OpenAI resource host for one tenant, built on request.

    The CLI asks for a host only after it has read the target's routing fresh
    (the channel's backend revision, or the agent's neutral catalog row) and
    found the OpenAI profile. The factory owns the provider credentials and
    workspace; the CLI never builds a provider client itself.
    """

    # The provider workspace the factory's hosts use, so routing can read the
    # neutral catalog before any host is requested.
    @property
    def account_scope_id(self) -> str: ...

    def __call__(
        self,
        *,
        tenant_id: uuid.UUID,
        account_id: uuid.UUID,
        principal_id: str,
        authorization_id: str,
        operation_key: str,
    ) -> NeutralResourceHost: ...


CLI_AUTHORIZATION_ID = "cli-deployment-operator"


def cli_principal_id(rt: CliRuntime) -> str:
    return f"cli:{rt.settings.cli.local_user}"


class ResourceHostMismatch(DaimonError):
    """The operator's factory returned a host for a different scope; nothing was read or written."""


def request_resource_host(
    rt: CliRuntime,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    workspace: str,
    operation_key: str,
) -> NeutralResourceHost:
    """The factory's host for one authorized deployment, bound to exactly that selection.

    Asking the factory for a tenant and account does not bind what it returns,
    so before any read or write the host's own tenant, account, principal,
    authorization, provider workspace and profile must equal the authorized
    selection. A mismatch refuses; callers never fall back past it.
    """
    from daimon.core.neutral_resources import NeutralResourceHost

    factory = rt.resource_hosts
    if factory is None:
        raise ResourceHostMismatch("no operator resource host is configured")
    principal_id = cli_principal_id(rt)
    host = factory(
        tenant_id=tenant_id,
        account_id=account_id,
        principal_id=principal_id,
        authorization_id=CLI_AUTHORIZATION_ID,
        operation_key=operation_key,
    )
    # Operator code: its return type is checked at runtime, not trusted.
    if not isinstance(cast(object, host), NeutralResourceHost):
        raise ResourceHostMismatch("the operator's resource host is not a neutral resource host")
    scope = host.scope
    capabilities = host.backend.capabilities()
    if (
        scope.tenant_id != str(tenant_id)
        or scope.account_id != str(account_id)
        or scope.principal_id != principal_id
        or scope.authorization_id != CLI_AUTHORIZATION_ID
        or scope.is_platform
        or scope.is_legacy_host_authorized
        or host.workspace != workspace
        or capabilities.provider != "openai"
        or capabilities.profile_id != "openai.persistent_workspace"
    ):
        raise ResourceHostMismatch(
            "the operator's resource host is for a different tenant, account or workspace; "
            "nothing was read or changed"
        )
    return host


@dataclass(frozen=True)
class CliRuntime:
    settings: Settings
    anthropic: AsyncAnthropic
    sessionmaker: async_sessionmaker[AsyncSession]
    deployment_default: DeploymentDefault
    resolver_cache: ResolverCache
    # The explicit operator deployment capability for OpenAI agents and channel
    # copies. None (the default, and all `build_runtime` provides) keeps every
    # command on its existing Anthropic path.
    resource_hosts: ResourceHostFactory | None = None


@asynccontextmanager
async def build_runtime(settings: Settings) -> AsyncIterator[CliRuntime]:
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
    async with AsyncAnthropic(
        api_key=settings.anthropic.api_key.get_secret_value(),
        base_url=str(settings.anthropic.base_url),
        max_retries=MA_MAX_RETRIES,
        http_client=DefaultAsyncHttpxClient(
            transport=SkillsRateLimitedTransport(settings.anthropic.skills_requests_per_minute)
        ),
    ) as anthropic:
        try:
            yield CliRuntime(
                settings=settings,
                anthropic=anthropic,
                sessionmaker=sessionmaker,
                deployment_default=deployment_default,
                resolver_cache=resolver_cache,
            )
        finally:
            await drain_outcomes()
            await engine.dispose()
