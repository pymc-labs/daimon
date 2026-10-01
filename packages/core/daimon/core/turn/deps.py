"""TurnDeps -- frozen DI bundle for the pre-turn admission sequence (D-04).

Built once per adapter runtime (Discord/Slack `build_runtime`, the scheduler,
the CLI) and threaded through `admit()` / `run_prepared_turn`. Carries every
dependency the pre-turn sequence needs that does NOT vary per turn -- the
per-turn values (tenant_id, platform, external_user_id, channel_id, now) are
passed as separate arguments instead.

No I/O in this module; `build_turn_deps` derives the bundle from settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.billing import BillingConfig
from daimon.core.config import McpSettings, Settings
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_resolver import ResolverCache
from daimon.core.scope import DeploymentDefault
from daimon.core.tool_safety import OPEN_TOOL_SAFETY, ToolSafetyPolicy
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
    tool_safety: ToolSafetyPolicy = OPEN_TOOL_SAFETY


def _reveal(secret: SecretStr | None) -> str | None:
    return secret.get_secret_value() if secret is not None else None


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
    crypto_keys = tuple(secret.get_secret_value() for secret in settings.crypto.keys)
    github = settings.github
    return TurnDeps(
        anthropic=anthropic,
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
        public_url=str(settings.mcp.public_url) if settings.mcp.public_url is not None else None,
        tool_safety=settings.tool_safety,
    )
