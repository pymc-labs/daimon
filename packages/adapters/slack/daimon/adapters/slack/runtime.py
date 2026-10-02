"""SlackRuntime -- DI bundle for the Slack adapter process."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.billing import BillingConfig, load_billing_config
from daimon.core.channel_admins import GroupMembersCache
from daimon.core.channel_budget_notice import drain_budget_notices
from daimon.core.config import Settings
from daimon.core.constants import MA_MAX_RETRIES
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.loader import parse_deployment_default
from daimon.core.ma_resolver import ResolverCache, new_resolver_cache
from daimon.core.mcp_oauth import McpTokenProbe, probe_bearer_token
from daimon.core.scope import DeploymentDefault
from daimon.core.skills.rate_limit import SkillsRateLimitedTransport
from daimon.core.turn.deps import TurnDeps, build_turn_deps
from daimon.core.turn.errors import AdmissionDenialReason
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from daimon.core.turn.outcomes import drain_outcomes
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
    # The MCP token form's live check; None (tests) means "do not probe".
    # Production wires `daimon.core.mcp_oauth.probe_bearer_token`.
    mcp_token_probe: McpTokenProbe | None = None
    # Channel admin user groups' members, kept briefly (`channel_admin_groups`).
    group_members: GroupMembersCache = field(default_factory=GroupMembersCache)


def resolve_bot_display_name(settings: Settings) -> str:
    """Use the configured Slack name, including when the Slack block is absent."""
    return settings.slack.bot_display_name if settings.slack is not None else "daimon"


SLACK_REFUSAL_NOUNS = RefusalNouns(
    scope="workspace", admin="a workspace admin", billing="`/billing`"
)


def admission_refusal_message(
    reason: AdmissionDenialReason, settings: Settings, *, in_dm: bool = False
) -> str:
    """The shared admission refusal in Slack's nouns, naming this deployment's bot."""
    return admission_refusal_text(
        reason,
        SLACK_REFUSAL_NOUNS,
        bot_name=escape_mrkdwn(resolve_bot_display_name(settings)),
        in_dm=in_dm,
    )


def responder_handle(settings: Settings) -> str:
    """The handle people mention this deployment by, for the turn controls.

    The MA agent is named `daimon` while the Slack app may be installed under
    whatever name the operator chose; the controls carry both under one
    responder so the difference is never read as a second agent.
    """
    return f"@{resolve_bot_display_name(settings)}"


@asynccontextmanager
async def build_runtime(settings: Settings) -> AsyncIterator[SlackRuntime]:
    engine = build_engine(
        str(settings.database.url),
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout,
    )
    sm = build_session_factory(
        engine,
        crypto_keys=tuple(k.get_secret_value() for k in settings.crypto.keys),
        allow_plaintext=settings.crypto.allow_plaintext,
    )
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
            http_client=DefaultAsyncHttpxClient(
                transport=SkillsRateLimitedTransport(settings.anthropic.skills_requests_per_minute)
            ),
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
                mcp_token_probe=probe_bearer_token,
            )
        finally:
            await drain_outcomes()
            await drain_budget_notices()
            await engine.dispose()
