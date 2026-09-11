"""DiscordDriver -- the Discord half of the PlatformDriver Protocol.

Builds a DiscordRuntime + DaimonBot from an injected MARouter-backed fake
AsyncAnthropic and drives a turn via the REAL `DaimonBot.on_message` entry
point (D-02) -- the full on_message -> _orchestrate -> run_turn chain,
including Discord's own gates (mention detection, tenant liveness,
per-tenant concurrency). `create_session` and `build_context_xml` are
boundary-stubbed (Discord-API-only concerns unrelated to MA billing/turn
wiring -- mirrors `tests/integration/test_discord_turn_e2e.py`); `run_turn`
itself is never patched (D-01).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage
from daimon.adapters.discord.agent_setup import write as discord_write
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime, build_turn_deps
from daimon.core.config import McpSettings, ThreadNamingSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.purge import AccountPurgeResult
from daimon.core.purge import purge_account as core_purge_account
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import AgentSpec
from daimon.core.stores.tenants import set_provision_status
from daimon.testing.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_BALANCE_BLOCKED_TEXT = (
    "This server's daimon credit is depleted. An admin can top up with `/billing`."
)
_CAP_BLOCKED_TEXT = (
    "Monthly usage cap reached for this guild. "
    "An admin can adjust the cap with `/billing` (when available)."
)


def _make_fake_session(
    *, session_id: str, agent_id: str, model_id: str
) -> BetaManagedAgentsSession:
    now = datetime.now(UTC)
    return BetaManagedAgentsSession(
        id=session_id,
        agent=BetaManagedAgentsSessionAgent(
            id=agent_id,
            mcp_servers=[],
            model=BetaManagedAgentsModelConfig(id=model_id),
            name="test-agent",
            skills=[],
            tools=[],
            type="agent",
            version=1,
        ),
        created_at=now,
        environment_id="env_parity_test",
        metadata={},
        resources=[],
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        type="session",
        updated_at=now,
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=[],
        outcome_evaluations=[],
    )


@dataclass
class DiscordDriver:
    """Drives turns through `DaimonBot.on_message`, the real Discord entry point."""

    param_id: str = "discord"

    def _make_runtime(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        *,
        billing_config: object | None = None,
    ) -> DiscordRuntime:
        settings = MagicMock()
        settings.mcp = McpSettings()
        settings.billing.markup = Decimal("1.0")
        settings.billing.signup_credit = Decimal("0")
        settings.crypto.keys = []
        settings.github.oauth_scopes = ()
        discord_settings = MagicMock()
        discord_settings.max_concurrent_turns_per_tenant = 100
        discord_settings.bot_display_name = "daimon"
        settings.discord = discord_settings
        settings.thread_naming = ThreadNamingSettings(enabled=False)
        anthropic = build_fake_anthropic(router.dispatch)
        resolver_cache = new_resolver_cache()
        deployment_default = DeploymentDefault(agent_name="test-agent", environment_name="test-env")
        return DiscordRuntime(
            settings=settings,
            anthropic=anthropic,
            sessionmaker=sessionmaker,
            notebook_rate_limiter=RateLimiter(max_requests=999),
            billing_config=billing_config,  # pyright: ignore[reportArgumentType]  # test-injected BillingConfig | None
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            turn_deps=build_turn_deps(
                settings,
                anthropic,
                sessionmaker,
                deployment_default=deployment_default,
                resolver_cache=resolver_cache,
                billing_config=billing_config,  # pyright: ignore[reportArgumentType]  # test-injected BillingConfig | None
            ),
        )

    def _make_bot(self, runtime: DiscordRuntime) -> DaimonBot:
        intents = discord.Intents.default()
        intents.message_content = True
        bot = DaimonBot(runtime=runtime, intents=intents)
        bot._connection.user = MagicMock(spec=discord.ClientUser)  # pyright: ignore[reportPrivateUsage]
        bot._connection.user.id = 999  # pyright: ignore[reportPrivateUsage]
        bot._connection.user.mentioned_in = MagicMock(return_value=True)  # pyright: ignore[reportPrivateUsage]
        return bot

    def _make_message(
        self, *, workspace_id: str, channel_id: str, user_id: str, text: str
    ) -> discord.Message:
        message = MagicMock(spec=discord.Message)
        message.content = f"<@999> {text}"
        message.author = MagicMock()
        message.author.bot = False
        message.author.id = int(user_id)
        message.author.display_name = "parity-user"
        message.guild = MagicMock(spec=discord.Guild)
        message.guild.id = int(workspace_id)
        message.guild.owner_id = int(user_id)

        thread = MagicMock(spec=discord.Thread)
        thread.id = int(channel_id)
        thread.parent_id = int(channel_id) - 1

        # message_ref must carry a real .id so lifecycle._message_ref is
        # non-None; edit must be an AsyncMock since _edit_message awaits it.
        message_ref = MagicMock()
        message_ref.id = 42
        message_ref.edit = AsyncMock()
        thread.send = AsyncMock(return_value=message_ref)

        message.channel = thread
        message.add_reaction = AsyncMock()
        message.attachments = []
        message.mentions = [SimpleNamespace(id=999)]
        message.created_at = datetime(2026, 6, 14, tzinfo=UTC)
        return message

    async def dispatch_turn(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        workspace_id: str,
        channel_id: str,
        user_id: str,
        text: str,
        billing_config: object | None = None,
    ) -> list[str]:
        runtime = self._make_runtime(sessionmaker, router, billing_config=billing_config)
        bot = self._make_bot(runtime)
        message = self._make_message(
            workspace_id=workspace_id, channel_id=channel_id, user_id=user_id, text=text
        )
        thread = cast(MagicMock, message.channel)

        with (
            patch("daimon.core.turn.prepare.create_session") as mock_create_session,
            patch("daimon.adapters.discord.bot.build_context_xml") as mock_build_context_xml,
        ):
            mock_create_session.return_value = _make_fake_session(
                session_id="sess_parity_test",
                agent_id="ag_parity_test",
                model_id="claude-sonnet-4-6",
            )
            mock_build_context_xml.return_value = (f"<user_query>{text}</user_query>", [])
            await bot.on_message(message)

        message_ref = cast(MagicMock, thread.send.return_value)
        posted: list[str] = []
        for call in thread.send.call_args_list:
            if call.args and isinstance(call.args[0], str):
                posted.append(call.args[0])
        for call in message_ref.edit.call_args_list:
            content = call.kwargs.get("content")
            if isinstance(content, str):
                posted.append(content)
        return posted

    def expected_blocked_text(self, kind: Literal["balance", "cap"]) -> str:
        return _BALANCE_BLOCKED_TEXT if kind == "balance" else _CAP_BLOCKED_TEXT

    async def delete_agent(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        name: str,
    ) -> None:
        runtime = self._make_runtime(sessionmaker, router)
        await discord_write.delete_agent(runtime, tenant_id=tenant_id, name=name)

    async def fork_agent(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        source_name: str,
        new_name: str,
        account_id: uuid.UUID,
    ) -> None:
        runtime = self._make_runtime(sessionmaker, router)
        source_spec = AgentSpec(name=source_name, model="claude-sonnet-4-6")
        await discord_write.fork_agent(
            runtime,
            tenant_id=tenant_id,
            source_spec=source_spec,
            new_name=new_name,
            account_id=account_id,
        )

    async def purge_account(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        account_id: uuid.UUID,
    ) -> AccountPurgeResult:
        return await core_purge_account(
            sm=sessionmaker,
            account_id=account_id,
            anthropic=build_fake_anthropic(router.dispatch),
        )

    async def uninstall(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        workspace_id: str,
    ) -> None:
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=workspace_id)
        await set_provision_status(sessionmaker, tenant_id=tenant_id, archive=True)
