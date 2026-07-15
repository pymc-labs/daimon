"""Preconditions and agent/environment resolution for session creation.

MA session creation itself lives in ``daimon.core.sessions.create_session``;
this module owns the "can we create?" and "which agent/env?" steps.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

import asyncpg.exceptions  # type: ignore[reportMissingTypeStubs]
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from daimon.core.defaults.provisioning import reconcile_tenant_defaults
from daimon.core.errors import DaimonError
from daimon.core.ma_resolver import (
    MAResolverMissError,
    ResolverCache,
    resolve_agent,
    resolve_environment,
)
from daimon.core.scope import (
    DeploymentDefault,
    ScopeContext,
    TenantConfigRow,
    TenantScopeRef,
)
from daimon.core.stores.scoped_config_read import get_scope, resolve
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BootstrapErrorKind = Literal[
    "db_not_migrated",
    "defaults_missing",
    "no_default_agent",
    "no_default_environment",
    "agent_not_found",
    "environment_not_found",
]


class SessionBootstrapError(DaimonError):
    def __init__(self, kind: BootstrapErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind: BootstrapErrorKind = kind
        self.message: str = message


async def check_preconditions(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    default: DeploymentDefault,
) -> None:
    try:
        async with sessionmaker() as s:
            raw = await get_scope(s, scope=TenantScopeRef(tenant_id=tenant_id))
    except (ProgrammingError, asyncpg.exceptions.UndefinedTableError) as err:
        raise SessionBootstrapError(
            "db_not_migrated",
            "database not migrated.\n  run: uv run alembic upgrade head\n"
            "  (with DAIMON_DATABASE_URL set to your target DB)",
        ) from err
    cfg = raw if isinstance(raw, TenantConfigRow) else None
    tenant_agent_name = cfg.agent_name if cfg is not None else None
    # A tenant is ready when an agent name resolves from EITHER an explicit
    # tenant_config row OR the injected deployment default (config.yaml). The
    # latter is the one-click path: a fresh tenant with zero config rows.
    if tenant_agent_name is None and default.agent_name is None:
        raise SessionBootstrapError(
            "defaults_missing",
            "system defaults not loaded.\n  run: daimon defaults apply\n"
            "  (or ask your administrator to run it in a shared deployment)",
        )


async def resolve_agent_and_environment(
    session_factory: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_flag: str | None,
    environment_flag: str | None,
    defaults_root: Path,
    default: DeploymentDefault,
    cache: ResolverCache,
) -> tuple[BetaManagedAgentsAgent, BetaEnvironment]:
    """Resolve names via scoped config, then ids via ma_resolver, then re-retrieve
    full SDK objects (downstream callers consume BetaManagedAgentsAgent /
    BetaEnvironment fields beyond .id). MAResolverMissError is mapped to the
    existing SessionBootstrapError taxonomy so caller UX is unchanged."""
    context = ScopeContext(account_id=account_id, tenant_id=tenant_id)
    async with session_factory() as session:
        resolved = await resolve(session, context=context, default=default)

    agent_name = agent_flag or resolved.agent_name
    env_name = environment_flag or resolved.environment_name

    if agent_name is None:
        raise SessionBootstrapError(
            "no_default_agent",
            "no default agent configured. run: daimon defaults apply, or pass --agent NAME",
        )
    if env_name is None:
        raise SessionBootstrapError(
            "no_default_environment",
            "no default environment configured. run: daimon defaults apply, "
            "or pass --environment NAME",
        )

    try:
        agent_id = await resolve_agent(
            anthropic,
            tenant_id=tenant_id,
            daimon_tag=agent_name,
            cached_id=None,
            apply_callable=lambda: reconcile_tenant_defaults(
                anthropic, defaults_root, tenant_id=tenant_id, public_url=None
            ),
            cache=cache,
        )
    except MAResolverMissError as err:
        raise SessionBootstrapError(
            "agent_not_found",
            f"no agent named {agent_name!r} found on MA for this tenant.",
        ) from err
    try:
        env_id = await resolve_environment(
            anthropic,
            tenant_id=tenant_id,
            daimon_tag=env_name,
            cached_id=None,
            apply_callable=lambda: reconcile_tenant_defaults(
                anthropic, defaults_root, tenant_id=tenant_id, public_url=None
            ),
            cache=cache,
        )
    except MAResolverMissError as err:
        raise SessionBootstrapError(
            "environment_not_found",
            f"no environment named {env_name!r} found on MA for this tenant.",
        ) from err
    # (a)-pattern re-retrieve: resolver returns the live MA id; we re-fetch the
    # full SDK objects so downstream code (create_session, name printing) has
    # access to fields beyond .id. Small TOCTOU window is acceptable for the CLI.
    agent = await anthropic.beta.agents.retrieve(agent_id)
    env = await anthropic.beta.environments.retrieve(env_id)
    return agent, env
