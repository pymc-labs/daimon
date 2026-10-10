"""TurnDeps -- frozen DI bundle for the pre-turn admission sequence (D-04).

Built once per adapter runtime (Discord/Slack `build_runtime`, the scheduler,
the CLI) and threaded through `admit()` / `run_prepared_turn`. Carries every
dependency the pre-turn sequence needs that does NOT vary per turn -- the
per-turn values (tenant_id, platform, external_user_id, channel_id, now) are
passed as separate arguments instead.

No I/O in this module; `build_turn_deps` derives the bundle from settings.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.billing import BillingConfig
from daimon.core.channel_admins import GroupMembersFor
from daimon.core.channel_budget_notice import BudgetNotifier
from daimon.core.config import GithubAppSettings, McpSettings, Settings, TurnSettings
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_resolver import ResolverCache
from daimon.core.mux_backend import TurnRuntime
from daimon.core.scope import DeploymentDefault
from daimon.core.session_preparation_gate import PreparationGate
from daimon.core.tool_safety import OPEN_TOOL_SAFETY, ToolSafetyPolicy
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class TurnDeps:
    """Frozen DI bundle shared by every turn stage.

    `fernet` is built ONCE here from `settings.crypto.keys` at adapter runtime
    construction -- this is the mechanism that makes a missing fernet on the
    Slack adapter impossible by construction (a caller can't forget to build
    it per turn). `resolver_cache` is likewise a single shared instance per
    adapter process (D-12): Slack stops constructing a fresh cache per turn
    and adopts Discord's <=300s TTL staleness semantics.
    """

    anthropic: AsyncAnthropic
    sessionmaker: async_sessionmaker[AsyncSession]
    deployment_default: DeploymentDefault
    resolver_cache: ResolverCache
    defaults_root: Path
    mcp: McpSettings
    billing_config: BillingConfig | None
    markup: Decimal
    fernet: MultiFernet | None
    github_fallback_pat: str | None
    github_app_id: str | None
    github_app_private_key: str | None
    public_url: str | None
    agent_github_app: GithubAppSettings | None = None
    tool_safety: ToolSafetyPolicy = OPEN_TOOL_SAFETY
    preparation_gate: PreparationGate = field(default_factory=lambda: PreparationGate(1))
    # Sends the channel budget notice; set by an adapter with a platform client.
    budget_notifier: BudgetNotifier | None = None
    # Re-checks a notice recipient's stored Slack group or Teams team; set with the notifier.
    group_members: GroupMembersFor | None = None
    budget_notices_off: frozenset[uuid.UUID] = frozenset()
    # Native MA client remains during the rollout; ancillary Messages calls
    # have a separate injection point and never go through Events/Sessions.
    messages: AsyncAnthropic | None = None
    backend: ManagedAgents | None = None
    backend_session_ref: Callable[[str, Scope], ResourceRef] | None = None
    turn_path: Literal["legacy", "mux"] | None = None
    # `DAIMON_TURN__CHANNEL_BACKENDS`: admission reads channel backend configuration.
    channel_backends: bool = False
    # Provider owners supply transports and durable stores; no credentials
    # or SDK clients are discovered by profile dispatch.
    turn_runtimes: Mapping[str, TurnRuntime] = field(default_factory=dict[str, TurnRuntime])


def _reveal(secret: SecretStr | None) -> str | None:
    return secret.get_secret_value() if secret is not None else None


def _preparation_limit(settings: Settings) -> int:
    """Use the configured bound; tolerate incomplete settings mocks in adapter tests."""
    configured: object = settings.database.preparation_concurrency
    if isinstance(configured, int) and configured > 0:
        return configured
    pool_size = cast(object, settings.database.pool_size)
    return max(1, pool_size // 2) if isinstance(pool_size, int) else 1


def _notices_off(settings: Settings) -> frozenset[uuid.UUID]:
    """Tenants that turned the budget notice off; tolerate settings mocks in adapter tests."""
    configured = cast(object, settings.budget_notices)
    if not isinstance(configured, dict):
        return frozenset()
    return frozenset(t for t, on in cast("dict[uuid.UUID, bool]", configured).items() if not on)


def build_turn_deps(
    settings: Settings,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    deployment_default: DeploymentDefault,
    resolver_cache: ResolverCache,
    billing_config: BillingConfig | None,
) -> TurnDeps:
    """Derive `TurnDeps` from `settings`, once per adapter runtime."""
    turn_settings = cast(object, settings.turn)
    crypto_keys = tuple(secret.get_secret_value() for secret in settings.crypto.keys)
    github = settings.github
    return TurnDeps(
        anthropic=anthropic,
        messages=anthropic,
        turn_path=turn_settings.path if isinstance(turn_settings, TurnSettings) else None,
        channel_backends=(
            turn_settings.channel_backends if isinstance(turn_settings, TurnSettings) else False
        ),
        sessionmaker=sessionmaker,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        defaults_root=settings.defaults_root,
        mcp=settings.mcp,
        billing_config=billing_config,
        markup=settings.billing.markup,
        fernet=build_multifernet(crypto_keys) if crypto_keys else None,
        github_fallback_pat=_reveal(github.fallback_pat),
        github_app_id=github.app_id,
        github_app_private_key=_reveal(github.app_private_key),
        agent_github_app=settings.github_app,
        public_url=str(settings.mcp.public_url) if settings.mcp.public_url is not None else None,
        tool_safety=settings.tool_safety,
        preparation_gate=PreparationGate(_preparation_limit(settings)),
        budget_notices_off=_notices_off(settings),
    )
