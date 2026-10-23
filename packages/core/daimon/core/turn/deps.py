"""TurnDeps -- frozen DI bundle for the pre-turn admission sequence.

Built once per adapter runtime (Discord/Slack `build_runtime`, the scheduler,
the CLI) and threaded through `admit()` / `run_prepared_turn`. Carries every
dependency the pre-turn sequence needs that does NOT vary per turn -- the
per-turn values (tenant_id, platform, external_user_id, channel_id, now) are
passed as separate arguments instead.

No I/O in this module; `build_turn_deps` derives the bundle from settings.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.billing import BillingConfig
from daimon.core.channel_budget_notice import BudgetNotifier
from daimon.core.channel_skills import PlatformSkillMemory
from daimon.core.config import (
    GithubAppSettings,
    McpSettings,
    ProgressPlansSettings,
    Settings,
    TurnSettings,
    load_turn_settings,
)
from daimon.core.defaults.loader import load_platform_skills
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_resolver import ResolverCache
from daimon.core.mux_backend import TurnRuntime
from daimon.core.native_admins import ServerAdminIds
from daimon.core.scope import DeploymentDefault
from daimon.core.scope_cascade import CascadeMode
from daimon.core.session_preparation_gate import PreparationGate
from daimon.core.specs import SkillRef
from daimon.core.tool_safety import OPEN_TOOL_SAFETY, ToolSafetyPolicy
from daimon.core.turn.running import RunningTurns
from daimon.core.turn.runtimes import RuntimeFactory, build_channel_runtime
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.state.store import StateStore
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.neutral_resources import WorkerPermissions
    from daimon.core.stores.domain import BackendAgent
    from daimon.core.turn.admission import AdmissionGrant


@dataclass(frozen=True)
class TurnDeps:
    """Frozen DI bundle shared by every turn stage.

    `fernet` is built ONCE here from `settings.crypto.keys` at adapter runtime
    construction -- this is the mechanism that makes a missing fernet on the
    Slack adapter impossible by construction (a caller can't forget to build
    it per turn). `resolver_cache` is likewise reused across turns for the
    lifetime of the adapter process. Its ID mappings and full agent/environment
    entries retain the shared resolver owner's separate expiration policies.
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
    # The notice's server admins, where the stored role is not the source (Teams).
    server_admins: ServerAdminIds | None = None
    budget_notices_off: frozenset[uuid.UUID] = frozenset()
    # Native MA client remains during the rollout; ancillary Messages calls
    # have a separate injection point and never go through Events/Sessions.
    messages: AsyncAnthropic | None = None
    backend: ManagedAgents | None = None
    backend_session_ref: Callable[[str, Scope], ResourceRef] | None = None
    turn_path: Literal["legacy", "mux"] | None = None
    # `DAIMON_TURN__CHANNEL_BACKENDS`: admission reads channel backend configuration.
    channel_backends: bool = False
    agent_backends: bool = False
    native_footer_cost: bool = False
    # Microsoft accounts are configured, so sessions ask before `microsoft_send`.
    microsoft_send: bool = False
    # Provider owners supply transports and durable stores; no credentials
    # or SDK clients are discovered by profile dispatch.
    turn_runtimes: Mapping[str, TurnRuntime] = field(default_factory=dict[str, TurnRuntime])
    # Built-in application composition resolves only an admitted explicit channel.
    turn_runtime_factory: RuntimeFactory | None = None
    # Only prepared mux turns use this; default composition uses Postgres.
    state_store: StateStore | None = None
    # Every turn inside `run_prepared_turn`, whatever started it; read by shutdown drains.
    running_turns: RunningTurns = field(default_factory=RunningTurns)
    fleet_enabled: bool = False
    # `DAIMON_BILLING__OPENAI_DAILY_MODEL_CAP_USD`: each OpenAI model's daily spend cap
    # per tenant. None (a bundle built by hand) checks nothing.
    openai_daily_model_cap_usd: Decimal | None = None
    # `DAIMON_PROGRESS_PLANS__MODE`: the deployment tier of the progress-plans cascade.
    # Disabled unless built from settings, so a bundle assembled by hand never reads it.
    progress_plans: CascadeMode = CascadeMode.DISABLED
    fleet_max_workers: int = 8
    fleet_root_grants: (
        Callable[[AdmissionGrant, BackendAgent], Awaitable[WorkerPermissions]] | None
    ) = None
    # Each default agent's platform-limited skills, read from `defaults_root`
    # once (`load_platform_skills`); admission adds them on their platforms.
    platform_skills: Mapping[str, tuple[SkillRef, ...]] = field(
        default_factory=dict[str, tuple[SkillRef, ...]]
    )
    # What admission learned about those skills in this process: the last ids
    # read, and the ones the provider refused (`create_ma_session`).
    platform_skill_memory: PlatformSkillMemory = field(default_factory=PlatformSkillMemory)

    @property
    def backend_selection_enabled(self) -> bool:
        """Use the settings owner's opt-in boundary for every turn stage."""
        if not self.channel_backends:
            return False
        from daimon.core import channel_backend

        return channel_backend.backend_selection_enabled(
            TurnSettings(
                path=self.turn_path or load_turn_settings().path,
                channel_backends=self.channel_backends,
            )
        )


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
    fleet_root_grants: Callable[[AdmissionGrant, BackendAgent], Awaitable[WorkerPermissions]]
    | None = None,
) -> TurnDeps:
    """Derive `TurnDeps` from `settings`, once per adapter runtime."""
    turn_settings = cast(object, settings.turn)
    crypto_keys = tuple(secret.get_secret_value() for secret in settings.crypto.keys)
    return TurnDeps(
        anthropic=anthropic,
        messages=anthropic,
        turn_runtime_factory=build_channel_runtime,
        turn_path=turn_settings.path if isinstance(turn_settings, TurnSettings) else None,
        channel_backends=(
            turn_settings.channel_backends if isinstance(turn_settings, TurnSettings) else False
        ),
        agent_backends=(
            turn_settings.agent_backends if isinstance(turn_settings, TurnSettings) else False
        ),
        native_footer_cost=(
            turn_settings.native_footer_cost if isinstance(turn_settings, TurnSettings) else False
        ),
        sessionmaker=sessionmaker,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        defaults_root=settings.defaults_root,
        mcp=settings.mcp,
        billing_config=billing_config,
        markup=settings.billing.markup,
        fernet=build_multifernet(crypto_keys) if crypto_keys else None,
        github_fallback_pat=None,
        github_app_id=settings.github_app.app_id,
        github_app_private_key=_reveal(settings.github_app.private_key),
        agent_github_app=settings.github_app,
        public_url=str(settings.mcp.public_url) if settings.mcp.public_url is not None else None,
        tool_safety=settings.tool_safety,
        microsoft_send=settings.microsoft_accounts.configured,
        preparation_gate=PreparationGate(_preparation_limit(settings)),
        budget_notices_off=_notices_off(settings),
        fleet_enabled=settings.fleet.enabled,
        openai_daily_model_cap_usd=settings.billing.openai_daily_model_cap_usd,
        progress_plans=(
            plans.mode
            if isinstance(plans := cast(object, settings.progress_plans), ProgressPlansSettings)
            else CascadeMode.DISABLED
        ),
        fleet_max_workers=settings.fleet.max_workers_per_run,
        fleet_root_grants=fleet_root_grants,
        platform_skills=load_platform_skills(
            settings.defaults_root, features=settings.configured_features
        ),
    )
