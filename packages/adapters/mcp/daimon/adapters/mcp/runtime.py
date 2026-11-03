"""Process-scoped collaborators captured by tool-registration closures.

FastMCP registers top-level functions, so tools can't take DB / client as
positional args. Closures over an McpRuntime are the injection point;
AuthIdentity flows separately via ctx.get_state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.slack_http import SlackHttpSession
from daimon.adapters.mcp.tools.microsoft._access import MicrosoftGraph
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.core.artifacts import ArtifactStore
from daimon.core.compute_provider import ComputeProvider
from daimon.core.config import DiscordSettings, Settings
from daimon.core.discord_membership import DiscordRestMembershipResolver
from daimon.core.ma_resolver import ResolverCache, new_resolver_cache
from daimon.core.membership import MembershipResolver
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from google import genai
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.adapters.mcp.tools._gemini_resources import GeminiSkillCapability
    from daimon.core.neutral_resources import NeutralResourceHost


class McpResourceHostFactory(Protocol):
    """An operator-supplied OpenAI resource host, requested only for an authorized deployment."""

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


@dataclass(frozen=True)
class McpRuntime:
    session_factory: async_sessionmaker[AsyncSession]
    client: AsyncAnthropic
    settings: Settings
    deployment_default: DeploymentDefault
    gemini_client: genai.Client | None = None
    notebook_rate_limiter: RateLimiter | None = None
    bundle_rate_limiter: RateLimiter | None = None
    fernet: MultiFernet | None = field(default=None)
    artifact_store: ArtifactStore | None = field(default=None)
    teams_client: TeamsBotClient | None = field(default=None)
    # Teams members' own Microsoft 365 access; None when not configured.
    microsoft: MicrosoftGraph | None = field(default=None)
    # Re-checks stored Slack groups and Teams teams outside a chat turn; None checks none.
    resolver_cache: ResolverCache = field(default_factory=new_resolver_cache)
    # None (tests) lets slack_sdk open a throwaway session per call.
    slack_http: SlackHttpSession | None = field(default=None)

    # The explicit operator deployment capability for OpenAI agents. None (the
    # default) keeps every tool on its existing Anthropic path.
    resource_hosts: McpResourceHostFactory | None = field(default=None)

    # Separate explicit capability; absent/disabled keeps existing dispatch.
    gemini_skill_capability: GeminiSkillCapability | None = field(default=None)

    membership_resolver: MembershipResolver | None = None
    compute_provider: ComputeProvider | None = None

    def __post_init__(self) -> None:
        if self.membership_resolver is None and isinstance(self.settings.discord, DiscordSettings):
            object.__setattr__(
                self,
                "membership_resolver",
                DiscordRestMembershipResolver(self.settings.discord.bot_token.get_secret_value()),
            )
