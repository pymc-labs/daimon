"""Tests for per-guild tenant resolution + the non-ready self-heal gate in DaimonBot.

- on_message derives tenant_id via derive_tenant_uuid + reads liveness via get_tenant_liveness,
  threading the per-message tenant_id into _orchestrate — guild A never sees guild B's tenant.
- The unified non-ready self-heal gate (D-03): an unprovisioned/archived guild triggers
  ensure_provisioning + replies "setting up"; a 'failed' guild spawns a background re-seed
  + replies "setting up" (the word "failed" is never user-visible); 'pending' replies
  "setting up"; only 'ready' proceeds to resolve_agent.
- The plain agent-resolve-miss path: MAResolverMissError on a ready guild collapses to the
  "no longer exists" message — no retry, no per-user-active fallthrough (deleted in D-05).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from anthropic.types.beta.beta_cloud_config import BetaCloudConfig
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_session import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage
from anthropic.types.beta.beta_packages import BetaPackages
from anthropic.types.beta.beta_unrestricted_network import BetaUnrestrictedNetwork
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid as _derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError, new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores.tenants import (
    get_tenant_liveness,
    set_provision_status,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _make_fake_session(session_id: str = "sess_test") -> BetaManagedAgentsSession:
    return BetaManagedAgentsSession(
        id=session_id,
        agent=BetaManagedAgentsSessionAgent(
            id="agent_test",
            mcp_servers=[],
            model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-5"),
            name="test-agent",
            skills=[],
            tools=[],
            type="agent",
            version=1,
        ),
        created_at="2026-04-28T00:00:00Z",
        environment_id="env_test",
        metadata={},
        resources=[],
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        type="session",
        updated_at="2026-04-28T00:00:00Z",
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=[],
        outcome_evaluations=[],  # pyright: ignore[reportCallIssue]
    )


def _make_fake_agent(name: str = "test-agent") -> BetaManagedAgentsAgent:
    return BetaManagedAgentsAgent(
        id="ag_test",
        version=1,
        name=name,
        type="agent",
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-5"),
        created_at=datetime(2026, 4, 28, tzinfo=UTC),
        updated_at=datetime(2026, 4, 28, tzinfo=UTC),
        mcp_servers=[],
        metadata={},
        skills=[],
        tools=[],
    )


def _make_fake_environment(name: str = "test-env") -> BetaEnvironment:
    return BetaEnvironment(
        id="env_test",
        name=name,
        type="environment",
        config=BetaCloudConfig(
            type="cloud",
            networking=BetaUnrestrictedNetwork(type="unrestricted"),
            packages=BetaPackages(apt=[], cargo=[], gem=[], go=[], npm=[], pip=[]),
        ),
        created_at="2026-04-28T00:00:00Z",
        updated_at="2026-04-28T00:00:00Z",
        description="",
        metadata={},
    )


def _make_runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> DiscordRuntime:
    # runtime no longer carries tenant_id (D-06); on_message resolves it per-message.
    from decimal import Decimal

    settings = MagicMock()
    settings.mcp.public_url = None
    settings.defaults_root = MagicMock()
    settings.billing.markup = Decimal("1.0")
    settings.billing.signup_credit = Decimal("0")
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 100  # effectively uncapped in tests
    settings.discord = discord_settings
    anthropic = AsyncMock()
    anthropic.beta.agents.retrieve = AsyncMock(return_value=_make_fake_agent())
    anthropic.beta.environments.retrieve = AsyncMock(return_value=_make_fake_environment())
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


def _make_bot(runtime: DiscordRuntime) -> DaimonBot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = DaimonBot(runtime=runtime, intents=intents)
    bot._connection.user = MagicMock(spec=discord.ClientUser)  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.id = 999  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.mentioned_in = MagicMock(return_value=True)  # pyright: ignore[reportPrivateUsage]
    return bot


def _make_channel_message(
    *,
    content: str = "<@999> hello",
    guild_id: int = 123456,
    channel_id: int = 789,
    author_id: int = 111,
) -> discord.Message:
    message = MagicMock(spec=discord.Message)
    message.content = content
    message.author = MagicMock()
    message.author.bot = False
    message.author.id = author_id
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = guild_id
    message.channel = MagicMock()
    message.channel.__class__ = discord.TextChannel
    message.channel.id = channel_id
    message.channel.send = AsyncMock()
    message.create_thread = AsyncMock()
    message.add_reaction = AsyncMock()
    message.attachments = []
    message.mentions = [SimpleNamespace(id=999)]
    return message


class TestAgentResolveMiss:
    """A miss on a ready guild collapses to the plain 'no longer exists' message —
    no retry, no per-user-active fallthrough (deleted in D-05)."""

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_non_user_active_miss_preserves_no_longer_exists_error(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """On a ready guild, an MAResolverMissError sends the plain 'no longer exists'
        message — a single resolve_config call, no retry, no session/turn."""
        from daimon.core.defaults.provisioning import provision_tenant

        guild_id = "710000001"
        await provision_tenant(db_session_factory, platform="discord", workspace_id=guild_id)
        tenant_id = _derive_tenant_uuid(platform="discord", workspace_id=guild_id)

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="legacy-bot",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_find_agent.side_effect = MAResolverMissError(
            kind="agent", tenant_id=tenant_id, daimon_tag="legacy-bot"
        )
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message(guild_id=int(guild_id))

        await bot.on_message(message)

        # No retry — resolve_config called exactly once.
        assert mock_resolve_config.call_count == 1, "agent-resolve miss must not trigger any retry"
        # No session created, no turn run — error path fired.
        mock_create_session.assert_not_called()
        mock_run_turn.assert_not_called()
        message.channel.send.assert_called_once()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        sent_text: str = message.channel.send.call_args[0][0]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
        assert "no longer exists" in sent_text, "should render the plain 'no longer exists' message"


class TestPerMessageTenantResolution:
    """on_message resolves TenantContext per-guild and threads tenant_id into _orchestrate
    (per_message_tenant invariant). Only 'ready' guilds proceed."""

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_on_message_resolves_tenant_per_guild(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from daimon.core.defaults.provisioning import provision_tenant

        guild_a = "700000001"
        guild_b = "700000002"
        await provision_tenant(db_session_factory, platform="discord", workspace_id=guild_a)
        await provision_tenant(db_session_factory, platform="discord", workspace_id=guild_b)
        tenant_a = _derive_tenant_uuid(platform="discord", workspace_id=guild_a)

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="ws-bot",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_create_session.return_value = _make_fake_session("sess-a")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message(guild_id=int(guild_a))
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 7001
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        # The ScopeContext threaded into resolve_config carries guild A's tenant_id,
        # never guild B's.
        mock_resolve_config.assert_called_once()
        scope = mock_resolve_config.call_args.kwargs["context"]
        assert scope.tenant_id == tenant_a, "must thread guild A's derived tenant_id, not B's"

    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_on_message_pending_guild_sends_setting_up(
        self,
        mock_resolve_config: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from daimon.core.defaults.provisioning import provision_tenant

        guild_id = "700000003"
        result = await provision_tenant(
            db_session_factory, platform="discord", workspace_id=guild_id
        )
        await set_provision_status(db_session_factory, tenant_id=result.tenant_id, status="pending")

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message(guild_id=int(guild_id))

        await bot.on_message(message)

        mock_resolve_config.assert_not_called()
        message.channel.send.assert_awaited_once()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        sent_text: str = message.channel.send.await_args[0][0]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
        assert "setting up" in sent_text.lower(), "pending guild must reply 'setting up'"  # pyright: ignore[reportUnknownMemberType]


class TestNonReadySelfHealGate:
    """failed/unprovisioned guilds self-heal (spawn a bg seed) and reply 'setting up';
    'failed' is never user-visible (provisioning_pending invariant)."""

    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_on_message_failed_guild_self_heals_and_sends_setting_up(
        self,
        mock_resolve_config: AsyncMock,
        mock_reconcile: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.defaults.report import ApplyReport

        mock_reconcile.return_value = ApplyReport()
        guild_id = "700000004"
        result = await provision_tenant(
            db_session_factory, platform="discord", workspace_id=guild_id
        )
        await set_provision_status(db_session_factory, tenant_id=result.tenant_id, status="failed")

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)
        # The follow-up post (✅/⚠️) is covered in test_on_guild_join; here the bare
        # MagicMock guild has no awaitable channel, so stub the post-back path.
        bot._post_to_guild = AsyncMock()  # type: ignore[method-assign]
        message = _make_channel_message(guild_id=int(guild_id))

        await bot.on_message(message)
        await _drain_bg_tasks(bot)

        mock_resolve_config.assert_not_called()
        mock_reconcile.assert_awaited(), "failed guild must trigger a background re-seed"  # pyright: ignore[reportUnusedExpression]
        message.channel.send.assert_awaited_once()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        sent_text: str = message.channel.send.await_args[0][0]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
        assert "setting up" in sent_text.lower()  # pyright: ignore[reportUnknownMemberType]
        assert "failed" not in sent_text.lower(), "'failed' must never be shown to the user"  # pyright: ignore[reportUnknownMemberType]

    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_on_message_unprovisioned_guild_self_heals(
        self,
        mock_resolve_config: AsyncMock,
        mock_reconcile: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from daimon.core.defaults.report import ApplyReport

        mock_reconcile.return_value = ApplyReport()
        guild_id = "700000005"  # NO tenant row — get_tenant_liveness returns None

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)
        bot._post_to_guild = AsyncMock()  # type: ignore[method-assign]
        message = _make_channel_message(guild_id=int(guild_id))

        await bot.on_message(message)
        await _drain_bg_tasks(bot)

        # ensure_provisioning ran: a tenant row now exists, and a bg seed was spawned.
        tenant_id = _derive_tenant_uuid(platform="discord", workspace_id=guild_id)
        tr = await get_tenant_liveness(db_session_factory, tenant_id)
        assert tr is not None, "unprovisioned guild must be provisioned by the self-heal gate"
        mock_reconcile.assert_awaited()
        mock_resolve_config.assert_not_called()
        message.channel.send.assert_awaited_once()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        sent_text: str = message.channel.send.await_args[0][0]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
        assert "setting up" in sent_text.lower()  # pyright: ignore[reportUnknownMemberType]


class TestEveryoneMentionIgnored:
    """@everyone / @here must NOT trigger the bot. discord.py's user.mentioned_in
    returns True for any mass ping (it short-circuits on message.mention_everyone);
    on_message gates on message.mentions instead, which excludes @everyone/@here."""

    @patch("daimon.adapters.discord.bot.derive_tenant_uuid")
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_on_message_everyone_ping_is_ignored(
        self,
        mock_resolve_config: AsyncMock,
        mock_derive_tenant: MagicMock,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)
        # @everyone ping: discord populates mention_everyone but the bot is NOT in
        # message.mentions. mentioned_in would still return True, so assert we don't
        # rely on it.
        bot._connection.user.mentioned_in = MagicMock(return_value=True)  # pyright: ignore[reportPrivateUsage]
        message = _make_channel_message(content="@everyone heads up")
        message.mentions = []  # pyright: ignore[reportAttributeAccessIssue]
        message.mention_everyone = True  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        mock_derive_tenant.assert_not_called(), "@everyone must not reach tenant resolution"  # pyright: ignore[reportUnusedExpression]
        mock_resolve_config.assert_not_called(), "@everyone must not start a turn"  # pyright: ignore[reportUnusedExpression]
        message.create_thread.assert_not_awaited()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


async def _drain_bg_tasks(bot: DaimonBot) -> None:
    while bot._bg_tasks:  # pyright: ignore[reportPrivateUsage]
        await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]
