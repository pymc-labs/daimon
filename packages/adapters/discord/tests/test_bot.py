"""Tests for DaimonBot in-flight concurrency cap, is_admin derivation,
per-caller session keying, and per-turn role upsert.

Plan 50-08: per-tenant in-flight counter + is_admin threading into SessionContext.
Plan 88-04: per-(thread,account) session keying (flag-gated) + unconditional role upsert.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault, ResolvedConfig
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


def _make_runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    max_concurrent_turns_per_tenant: int = 3,
    per_caller_thread_sessions: bool = False,
) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.defaults_root = MagicMock()
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = max_concurrent_turns_per_tenant
    discord_settings.per_caller_thread_sessions = per_caller_thread_sessions
    settings.discord = discord_settings
    anthropic = AsyncMock()
    anthropic.beta.agents.retrieve = AsyncMock()
    anthropic.beta.environments.retrieve = AsyncMock()
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
    author: discord.abc.User | None = None,
) -> discord.Message:
    message = MagicMock(spec=discord.Message)
    message.content = content
    if author is not None:
        message.author = author
    else:
        message.author = MagicMock()
        message.author.bot = False
        message.author.id = author_id
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = guild_id
    message.guild.owner_id = 9999
    message.channel = MagicMock()
    message.channel.__class__ = discord.TextChannel
    message.channel.id = channel_id
    message.channel.send = AsyncMock()
    message.create_thread = AsyncMock()
    message.add_reaction = AsyncMock()
    message.mentions = [SimpleNamespace(id=999)]
    return message


class TestInflightCapRejection:
    """4th turn for a saturated tenant rejected (SCALE-01)."""

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_on_message_rejects_over_cap_for_tenant(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """4th turn for a saturated tenant rejected (SCALE-01).

        Pre-seed inflight count at the cap; the next on_message call must send
        the over-cap message and NOT start a turn.
        """
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid

        guild_id = "801000001"
        cap = 3
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("5.00"),
        )
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

        runtime = _make_runtime(db_session_factory, max_concurrent_turns_per_tenant=cap)
        bot = _make_bot(runtime)

        # Saturate the tenant's in-flight slot.
        bot._inflight[tenant_id] = cap  # pyright: ignore[reportPrivateUsage]

        message = _make_channel_message(guild_id=int(guild_id))

        await bot.on_message(message)

        message.channel.send.assert_called_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        sent_text: str = message.channel.send.call_args[0][0]  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]
        assert "too many chats in flight" in sent_text, (
            f"over-cap reply must contain 'too many chats in flight'; got: {sent_text!r}"
        )
        # Verify it's a plain send — no ephemeral kwarg.
        call_kwargs = message.channel.send.call_args.kwargs  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]
        assert "ephemeral" not in call_kwargs, (
            "over-cap send must not have ephemeral kwarg (invalid for on_message)"
        )
        mock_create_session.assert_not_called(), "no session must be created for over-cap"  # pyright: ignore[reportUnusedExpression]
        mock_run_turn.assert_not_called(), "no turn must run for over-cap"  # pyright: ignore[reportUnusedExpression]


class TestInflightDecrement:
    """In-flight counter released on success and error."""

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_inflight_decrements_after_successful_turn(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """In-flight counter released after a successful turn."""
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid

        guild_id = "801000002"
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("5.00"),
        )
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_create_session.return_value = _make_fake_session("sess-dec")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message(guild_id=int(guild_id))
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 8001
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        # After the turn completes, tenant_id must be absent (or 0) from _inflight.
        count_after = bot._inflight.get(tenant_id, 0)  # pyright: ignore[reportPrivateUsage]
        assert count_after == 0, (
            f"in-flight counter must be 0 after a successful turn; got {count_after}"
        )

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_inflight_decrements_after_failed_turn(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """In-flight counter released even when the turn raises (finally bracket)."""
        import anthropic as _anthropic
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid

        guild_id = "801000003"
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("5.00"),
        )
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_create_session.return_value = _make_fake_session("sess-dec-err")
        mock_run_turn.side_effect = _anthropic.APIConnectionError(request=MagicMock())
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message(guild_id=int(guild_id))
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 8002
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        count_after = bot._inflight.get(tenant_id, 0)  # pyright: ignore[reportPrivateUsage]
        assert count_after == 0, (
            f"in-flight counter must be 0 after a failed turn; got {count_after}"
        )


class TestInflightIsolation:
    """Per-tenant isolation: A saturated, B unaffected."""

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_inflight_isolated_per_tenant(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Per-tenant isolation: tenant A saturated, tenant B still admits."""
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid

        guild_a = "801000010"
        guild_b = "801000011"
        cap = 3
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_a,
            signup_credit=Decimal("5.00"),
        )
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_b,
            signup_credit=Decimal("5.00"),
        )
        tenant_a = derive_tenant_uuid(platform="discord", workspace_id=guild_a)

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_create_session.return_value = _make_fake_session("sess-isolation")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory, max_concurrent_turns_per_tenant=cap)
        bot = _make_bot(runtime)

        # Saturate only guild A.
        bot._inflight[tenant_a] = cap  # pyright: ignore[reportPrivateUsage]

        # Guild A message: must be rejected.
        message_a = _make_channel_message(guild_id=int(guild_a), channel_id=7010)
        await bot.on_message(message_a)

        message_a.channel.send.assert_called_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        sent_a: str = message_a.channel.send.call_args[0][0]  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]
        assert "too many chats in flight" in sent_a, (
            f"guild A (saturated) should be rejected; got: {sent_a!r}"
        )
        mock_create_session.assert_not_called(), "guild A must not create a session"  # pyright: ignore[reportUnusedExpression]

        # Guild B message: must proceed (separate counter).
        message_b = _make_channel_message(guild_id=int(guild_b), channel_id=7011)
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 8010
        mock_thread.send = AsyncMock()
        message_b.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message_b)

        (  # pyright: ignore[reportUnusedExpression]
            mock_create_session.assert_called_once(),
            ("guild B (unsaturated) must create a session; per-tenant isolation"),
        )


class TestIsAdminDerivation:
    """is_admin derived from manage_guild writes the live DB role.

    Prior to this change, is_admin was threaded into SessionContext and baked into
    the long-lived vault credential. The new design removes the SessionContext baking
    (credential is now identity-stable) and instead writes the live
    DB account.role each turn — the MCP gate then reads it live.

    These tests verify that manage_guild derives correctly and that create_session
    is no longer passed session_context.
    """

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_manage_guild_member_writes_admin_role_to_db(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Member with manage_guild=True → account.role=admin written to the DB each turn.

        The current design replaces the defunct SessionContext(is_admin=...) baking with a
        live DB role write. The MCP gate reads account.role on every request (88-03).
        """
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.stores.accounts import get_account
        from daimon.core.stores.domain import Role
        from daimon.core.stores.identity import get_or_create_platform_principal

        guild_id = "801000020"
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("5.00"),
        )
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_create_session.return_value = _make_fake_session("sess-admin-true")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)

        # Member with manage_guild=True.
        admin_member = MagicMock(spec=discord.Member)
        admin_member.bot = False
        admin_member.id = 555
        admin_member.guild_permissions = MagicMock()
        admin_member.guild_permissions.manage_guild = True
        admin_member.guild_permissions.administrator = False

        message = _make_channel_message(
            guild_id=int(guild_id), channel_id=9020, author=admin_member
        )
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9021
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        # is_admin drives a live DB role write, not SessionContext baking.
        mock_create_session.assert_called_once()
        assert "session_context" not in mock_create_session.call_args.kwargs, (
            "create_session must NOT receive session_context — credential is now identity-stable; "
            "credential identity-stable; admin gate reads live DB role (88-04)"
        )

        # The account.role must be admin in the DB.
        async with db_session_factory() as s:
            principal = await get_or_create_platform_principal(
                s, tenant_id=tenant_id, platform="discord", external_id="555"
            )
            await s.commit()
        async with db_session_factory() as s:
            account = await get_account(s, principal.account_id)
        assert account is not None and account.role == Role.ADMIN, (
            "manage_guild=True member must write account.role=admin to the DB each turn"
        )

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_non_manage_guild_member_writes_user_role_to_db(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Non-manage_guild member → account.role=user written to the DB each turn."""
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.stores.accounts import get_account
        from daimon.core.stores.domain import Role
        from daimon.core.stores.identity import get_or_create_platform_principal

        guild_id = "801000021"
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("5.00"),
        )
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_create_session.return_value = _make_fake_session("sess-admin-false")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)

        # Regular member without manage_guild.
        regular_member = MagicMock(spec=discord.Member)
        regular_member.bot = False
        regular_member.id = 666
        regular_member.guild_permissions = MagicMock()
        regular_member.guild_permissions.manage_guild = False
        regular_member.guild_permissions.administrator = False

        message = _make_channel_message(
            guild_id=int(guild_id), channel_id=9030, author=regular_member
        )
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9031
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        # No session_context in create_session call.
        mock_create_session.assert_called_once()
        assert "session_context" not in mock_create_session.call_args.kwargs, (
            "create_session must NOT receive session_context"
        )

        async with db_session_factory() as s:
            principal = await get_or_create_platform_principal(
                s, tenant_id=tenant_id, platform="discord", external_id="666"
            )
            await s.commit()
        async with db_session_factory() as s:
            account = await get_account(s, principal.account_id)
        assert account is not None and account.role == Role.USER, (
            "non-manage_guild member must write account.role=user to the DB each turn"
        )

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_plain_user_not_member_writes_user_role_to_db(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Plain discord.User (not Member) defaults to is_admin=False → role=user in DB."""
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.stores.accounts import get_account
        from daimon.core.stores.domain import Role
        from daimon.core.stores.identity import get_or_create_platform_principal

        guild_id = "801000022"
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("5.00"),
        )
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_create_session.return_value = _make_fake_session("sess-user-admin")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)

        # Plain User (not a Member — no guild_permissions attribute).
        plain_user = MagicMock(spec=discord.User)
        plain_user.bot = False
        plain_user.id = 777

        message = _make_channel_message(guild_id=int(guild_id), channel_id=9040, author=plain_user)
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9041
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        mock_create_session.assert_called_once()
        assert "session_context" not in mock_create_session.call_args.kwargs, (
            "create_session must NOT receive session_context"
        )

        async with db_session_factory() as s:
            principal = await get_or_create_platform_principal(
                s, tenant_id=tenant_id, platform="discord", external_id="777"
            )
            await s.commit()
        async with db_session_factory() as s:
            account = await get_account(s, principal.account_id)
        assert account is not None and account.role == Role.USER, (
            "plain User (not Member) must write account.role=user — isinstance(author, discord.Member) "
            "is False so is_admin=False"
        )


def _make_sweep_guild(guild_id: int) -> MagicMock:
    """Build a minimal discord.Guild stub suitable for on_ready sweep tests."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = "Sweep Guild"
    me = MagicMock(spec=discord.Member)
    me.guild_permissions = MagicMock()
    me.guild_permissions.manage_guild = False
    me.guild_permissions.administrator = False
    # Sendable system channel so _post_to_guild succeeds without DM fallback.
    sys_ch = MagicMock()
    perms = MagicMock()
    perms.send_messages = True
    sys_ch.permissions_for = MagicMock(return_value=perms)
    sys_ch.send = AsyncMock()
    guild.me = me
    guild.system_channel = sys_ch
    guild.text_channels = []
    guild.owner = None
    guild.owner_id = None
    return guild


class TestOnReadySweepProvisioning:
    """on_ready sweep provision branch passes clear_archive=True and
    posts the welcome embed before spawning the seed (#144-3)."""

    @patch("daimon.adapters.discord.bot.set_provision_status", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.provision_tenant", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.list_tenants_by_platform", new_callable=AsyncMock)
    async def test_sweep_provision_passes_clear_archive(
        self,
        mock_list_tenants: AsyncMock,
        mock_provision: AsyncMock,
        mock_reconcile: AsyncMock,
        mock_set_provision_status: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """on_ready sweep provision-new-guild branch must pass clear_archive=True to
        set_provision_status — defense-in-depth for guilds that rejoined while the bot
        was down."""
        import uuid

        guild_id = 900000001
        tenant_id = uuid.uuid4()

        # Simulate no known tenants so the sweep triggers the provision branch.
        mock_list_tenants.return_value = []
        mock_provision.return_value = MagicMock(tenant_id=tenant_id)

        from daimon.core.defaults.report import ApplyReport

        mock_reconcile.return_value = ApplyReport()

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)

        # Register one guild that is not in known_guild_ids.
        mock_guild = MagicMock(spec=discord.Guild)
        mock_guild.id = guild_id
        mock_guild.name = "Sweep Guild"
        me = MagicMock(spec=discord.Member)
        perms = MagicMock()
        perms.send_messages = True
        me.guild_permissions = MagicMock()
        me.guild_permissions.manage_guild = False
        me.guild_permissions.administrator = False
        mock_guild.me = me
        mock_guild.system_channel = None
        mock_guild.text_channels = []
        mock_guild.owner = None
        mock_guild.owner_id = None
        bot._connection._guilds = {guild_id: mock_guild}  # pyright: ignore[reportPrivateUsage]

        # stub tree methods
        bot.tree.clear_commands = MagicMock()  # type: ignore[method-assign]
        bot.tree.sync = AsyncMock()  # type: ignore[method-assign]

        await bot.on_ready()

        # Find the set_provision_status call for the sweep provision branch
        # (status="pending" with clear_archive=True).
        pending_calls = [
            c
            for c in mock_set_provision_status.await_args_list
            if c.kwargs.get("status") == "pending"
        ]
        assert len(pending_calls) >= 1, (
            "sweep provision branch must call set_provision_status with status='pending'"
        )
        for call in pending_calls:
            assert call.kwargs.get("clear_archive") is True, (
                "#132: sweep provision branch must pass clear_archive=True to "
                "set_provision_status; got: " + repr(call.kwargs)
            )

    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.provision_tenant", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.list_tenants_by_platform", new_callable=AsyncMock)
    async def test_sweep_provision_posts_welcome_embed_before_seed(
        self,
        mock_list_tenants: AsyncMock,
        mock_provision: AsyncMock,
        mock_reconcile: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """#144-3: sweep-provisioned guilds must receive the welcome embed before the
        seed's terminal (ready/snag) embed — identical to on_guild_join ordering."""
        import uuid

        from daimon.core.defaults.report import ApplyReport

        guild_id = 900000002
        tenant_id = uuid.uuid4()

        mock_list_tenants.return_value = []
        mock_provision.return_value = MagicMock(tenant_id=tenant_id)
        mock_reconcile.return_value = ApplyReport()

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)

        mock_guild = _make_sweep_guild(guild_id)
        bot._connection._guilds = {guild_id: mock_guild}  # pyright: ignore[reportPrivateUsage]
        bot.tree.clear_commands = MagicMock()  # type: ignore[method-assign]
        bot.tree.sync = AsyncMock()  # type: ignore[method-assign]

        await bot.on_ready()
        # Drain bg tasks so the terminal embed is posted.
        while bot._bg_tasks:  # pyright: ignore[reportPrivateUsage]
            import asyncio as _asyncio

            await _asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]

        sys_ch = mock_guild.system_channel
        assert sys_ch.send.await_count >= 2, (  # pyright: ignore[reportUnknownMemberType]
            "sweep-provisioned guild must receive at least the welcome embed + terminal embed"
        )
        first_embed = sys_ch.send.await_args_list[0].kwargs["embed"]  # pyright: ignore[reportUnknownMemberType]
        first_text = (first_embed.title or "") + (first_embed.description or "")
        assert "setting up" in first_text.lower(), (
            "#144-3: sweep provision must post the welcome 'setting up' embed first, "
            f"before the terminal embed; got: {first_text!r}"
        )
        last_embed = sys_ch.send.await_args_list[-1].kwargs["embed"]  # pyright: ignore[reportUnknownMemberType]
        last_text = (last_embed.title or "") + (last_embed.description or "")
        assert "ready" in last_text.lower() or "snag" in last_text.lower(), (
            "final embed must be the terminal ready/snag embed"
        )


def _make_thread_message_for_bot(
    *,
    content: str = "<@999> hello",
    guild_id: int = 123456,
    thread_id: int = 5555,
    parent_id: int = 789,
    author_id: int = 111,
    author: discord.abc.User | None = None,
) -> discord.Message:
    """Mock a message arriving in an existing Discord thread (bot tests)."""
    message = MagicMock(spec=discord.Message)
    message.content = content
    if author is not None:
        message.author = author
    else:
        message.author = MagicMock()
        message.author.bot = False
        message.author.id = author_id
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = guild_id
    message.guild.owner_id = 9999
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    thread.send = AsyncMock()
    thread.history = MagicMock(return_value=_AsyncIter([]))
    message.channel = thread
    message.add_reaction = AsyncMock()
    message.attachments = []
    message.mentions = [SimpleNamespace(id=999)]
    return message


class _AsyncIter:
    """Minimal async iterator for history stubs in bot tests."""

    def __init__(self, items: list[discord.Message]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> discord.Message:
        try:
            return next(self._items)
        except StopIteration as err:
            raise StopAsyncIteration from err


class TestPerCallerSessionKeying:
    """Per-(thread,account) session keying with flag-gated sentinel.

    Flag ON:  get_live_thread_session + both create_thread_session calls use
              session_account_id=principal.account_id → distinct callers get
              distinct sessions.
    Flag OFF: all callers in one thread share the deterministic legacy sentinel →
              single session per thread, identical to pre-88-04 behavior.
    """

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_low_priv_caller_in_admin_starter_thread_gets_own_session_when_distinct_external_ids(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_build_context_xml: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Flag ON: a distinct low-priv caller in an admin-started thread cold-creates
        their own session — they never reuse the starter's session row (T-88-04-01).

        Uses DISTINCT external_ids (admin=111, low-priv=222) to ensure the test
        covers the cross-account gap that a same-external_id test would mask.
        """
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.stores.identity import get_or_create_platform_principal
        from daimon.core.stores.thread_sessions import get_live_thread_session

        guild_id = "802000001"
        thread_id = 8020001
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("100.00"),
        )
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        mock_build_context_xml.return_value = ("<context></context>", [])

        # Admin caller (external_id=111) starts the thread: create_session fires, row inserted.
        mock_create_session.return_value = _make_fake_session("sess-admin-starter")
        runtime = _make_runtime(db_session_factory, per_caller_thread_sessions=True)
        bot = _make_bot(runtime)

        admin_member = MagicMock(spec=discord.Member)
        admin_member.bot = False
        admin_member.id = 111
        admin_member.guild_permissions = MagicMock()
        admin_member.guild_permissions.manage_guild = True
        admin_member.guild_permissions.administrator = False

        starter_message = _make_thread_message_for_bot(
            guild_id=int(guild_id),
            thread_id=thread_id,
            author=admin_member,
        )
        await bot.on_message(starter_message)

        # Resolve admin's account_id so we can verify the DB state.
        async with db_session_factory() as s:
            admin_principal = await get_or_create_platform_principal(
                s, tenant_id=tenant_id, platform="discord", external_id="111"
            )
            await s.commit()
        admin_account_id = admin_principal.account_id

        # Verify admin's session row was written.
        async with db_session_factory() as s:
            admin_row = await get_live_thread_session(
                s,
                tenant_id=tenant_id,
                platform="discord",
                thread_id=str(thread_id),
                account_id=admin_account_id,
            )
        assert admin_row is not None, (
            "admin starter's session row must exist in thread_sessions with their account_id"
        )

        # Now a DISTINCT low-priv caller (external_id=222) mentions in the same thread.
        mock_create_session.return_value = _make_fake_session("sess-lowpriv-caller")
        mock_create_session.reset_mock()

        low_priv = MagicMock(spec=discord.Member)
        low_priv.bot = False
        low_priv.id = 222  # DISTINCT from admin's external_id=111
        low_priv.guild_permissions = MagicMock()
        low_priv.guild_permissions.manage_guild = False
        low_priv.guild_permissions.administrator = False

        caller_message = _make_thread_message_for_bot(
            guild_id=int(guild_id),
            thread_id=thread_id,
            author=low_priv,
        )
        await bot.on_message(caller_message)

        # Low-priv must have cold-created a new session (not reused the admin's).
        assert mock_create_session.call_count == 1, (
            "distinct low-priv caller must cold-create their own session "
            "(their account_id does not match the admin starter's row); "
            f"got {mock_create_session.call_count} create_session calls"
        )

        # Verify low-priv's session row has THEIR account_id, not the admin's.
        async with db_session_factory() as s:
            low_priv_principal = await get_or_create_platform_principal(
                s, tenant_id=tenant_id, platform="discord", external_id="222"
            )
            await s.commit()
        low_priv_account_id = low_priv_principal.account_id

        assert low_priv_account_id != admin_account_id, (
            "distinct external_ids must produce distinct account_ids (test sanity)"
        )

        async with db_session_factory() as s:
            low_priv_row = await get_live_thread_session(
                s,
                tenant_id=tenant_id,
                platform="discord",
                thread_id=str(thread_id),
                account_id=low_priv_account_id,
            )
        assert low_priv_row is not None, (
            "low-priv caller's session row must exist with their own account_id, "
            "not the admin starter's"
        )
        assert low_priv_row.account_id == low_priv_account_id, (
            "session row must carry the low-priv caller's account_id, not the admin's"
        )

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_recreate_path_persists_account_id_on_new_row(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_build_context_xml: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Flag ON: when run_turn returns a dead-session 404, the recreate path
        inserts a new thread_sessions row with the caller's account_id — not NULL
        (T-88-04-02: NULL row → permanent cold-create loop).
        """
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.stores.identity import get_or_create_platform_principal
        from daimon.core.stores.thread_sessions import get_live_thread_session
        from daimon.core.turn.state import TurnState

        guild_id = "802000002"
        thread_id = 8020002
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("100.00"),
        )
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        mock_build_context_xml.return_value = ("<context></context>", [])

        # First call to run_turn returns a dead-session state; second call succeeds.
        import anthropic as _anthropic  # noqa: PLC0415
        from daimon.core.errors import TurnError  # noqa: PLC0415

        fake_response = MagicMock()
        fake_response.status_code = 404
        api_404 = _anthropic.NotFoundError(
            response=fake_response,
            message="not_found_error",
            body={"type": "not_found_error"},
        )
        dead_error = TurnError(kind="upstream", cause=api_404)
        dead_state = MagicMock(spec=TurnState)
        dead_state.error = dead_error

        alive_state = MagicMock(spec=TurnState)
        alive_state.error = None

        mock_run_turn.side_effect = [dead_state, alive_state]
        mock_create_session.side_effect = [
            _make_fake_session("sess-dead-original"),
            _make_fake_session("sess-recreated"),
        ]

        runtime = _make_runtime(db_session_factory, per_caller_thread_sessions=True)
        bot = _make_bot(runtime)

        caller = MagicMock(spec=discord.Member)
        caller.bot = False
        caller.id = 333
        caller.guild_permissions = MagicMock()
        caller.guild_permissions.manage_guild = False
        caller.guild_permissions.administrator = False

        message = _make_thread_message_for_bot(
            guild_id=int(guild_id),
            thread_id=thread_id,
            author=caller,
        )
        await bot.on_message(message)

        # Resolve the caller's account_id.
        async with db_session_factory() as s:
            caller_principal = await get_or_create_platform_principal(
                s, tenant_id=tenant_id, platform="discord", external_id="333"
            )
            await s.commit()
        caller_account_id = caller_principal.account_id

        # The recreated row must carry the caller's account_id (not NULL).
        async with db_session_factory() as s:
            recreated_row = await get_live_thread_session(
                s,
                tenant_id=tenant_id,
                platform="discord",
                thread_id=str(thread_id),
                account_id=caller_account_id,
            )
        assert recreated_row is not None, (
            "recreate path must insert a thread_sessions row with the caller's account_id; "
            "a NULL row would never match a subsequent turn and cause a permanent cold-create loop "
            "(T-88-04-02)"
        )
        assert recreated_row.account_id == caller_account_id, (
            "recreated session row must carry the caller's account_id, not NULL"
        )
        assert recreated_row.ma_session_id == "sess-recreated", (
            "recreated row must reference the new MA session, not the dead one"
        )

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_legacy_flag_off_reuses_single_session_per_thread(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_build_context_xml: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Flag OFF: two distinct callers in one thread reuse the SAME session —
        the deterministic legacy sentinel makes every caller use the same lookup key,
        preserving pre-88-04 behavior exactly.
        """
        from daimon.core.defaults.provisioning import provision_tenant

        guild_id = "802000003"
        thread_id = 8020003
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("100.00"),
        )

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        mock_build_context_xml.return_value = ("<context></context>", [])
        mock_create_session.return_value = _make_fake_session("sess-legacy-shared")

        # Flag OFF → legacy single-session-per-thread.
        runtime = _make_runtime(db_session_factory, per_caller_thread_sessions=False)
        bot = _make_bot(runtime)

        # First caller (external_id=444) starts the thread.
        caller_a = MagicMock(spec=discord.Member)
        caller_a.bot = False
        caller_a.id = 444
        caller_a.guild_permissions = MagicMock()
        caller_a.guild_permissions.manage_guild = False
        caller_a.guild_permissions.administrator = False

        message_a = _make_thread_message_for_bot(
            guild_id=int(guild_id), thread_id=thread_id, author=caller_a
        )
        await bot.on_message(message_a)

        # Second DISTINCT caller (external_id=555) mentions in the same thread.
        caller_b = MagicMock(spec=discord.Member)
        caller_b.bot = False
        caller_b.id = 555  # DISTINCT from 444
        caller_b.guild_permissions = MagicMock()
        caller_b.guild_permissions.manage_guild = False
        caller_b.guild_permissions.administrator = False

        message_b = _make_thread_message_for_bot(
            guild_id=int(guild_id), thread_id=thread_id, author=caller_b
        )
        await bot.on_message(message_b)

        # With flag OFF, only one session must have been created (the second turn reuses it).
        assert mock_create_session.call_count == 1, (
            "flag OFF: two distinct callers in the same thread must reuse ONE session "
            "(the legacy (tenant,platform,thread) key is shared via the deterministic sentinel); "
            f"got {mock_create_session.call_count} create_session calls"
        )

    async def test_legacy_sentinel_never_matches_a_real_account_id(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The OFF-path legacy sentinel uuid5 is collision-proof: it is never equal
        to any real account.id row in the DB (accounts use random uuid4 from Account()).

        This guarantees the sentinel cannot accidentally match a real caller's account,
        which would break the OFF→ON migration invariant (W1).
        """
        from daimon.core.stores.accounts import account_exists

        # Compute the sentinel formula for an arbitrary (tenant_id, thread_id).
        tenant_id = uuid.uuid4()
        thread_id = 99999

        sentinel = uuid.uuid5(uuid.NAMESPACE_URL, f"legacy-thread-sentinel:{tenant_id}:{thread_id}")

        # Insert a couple of real accounts and confirm the sentinel is not among them.
        async with db_session_factory() as s:
            from daimon.testing.factories import make_account, make_tenant  # noqa: PLC0415

            tenant = await make_tenant(s)
            acct1 = await make_account(s, tenant=tenant)
            acct2 = await make_account(s, tenant=tenant)
            await s.commit()

        real_ids = {acct1.id, acct2.id}
        assert sentinel not in real_ids, (
            f"legacy sentinel {sentinel} must never equal any real account uuid4; "
            f"real ids: {real_ids}"
        )

        # Also assert via the DB that the sentinel account does not exist.
        async with db_session_factory() as s:
            sentinel_exists = await account_exists(s, account_id=sentinel)
        assert not sentinel_exists, (
            f"legacy sentinel {sentinel} must not exist as an accounts row — "
            "it is a uuid5 derived from NAMESPACE_URL, accounts use uuid4 (W1)"
        )


class TestPerTurnRoleUpsert:
    """Per-turn unconditional account.role upsert from Discord admin perms.

    The role write runs BEFORE run_turn and is NOT gated by per_caller_thread_sessions.
    It targets only the platform-principal's account — never CLI/operator accounts (T-88-04-03).
    """

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_admin_turn_upserts_account_role_admin(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An admin caller's turn sets their platform account role to admin in the DB."""
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.stores.accounts import get_account
        from daimon.core.stores.domain import Role
        from daimon.core.stores.identity import get_or_create_platform_principal

        guild_id = "803000001"
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("100.00"),
        )
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_create_session.return_value = _make_fake_session("sess-role-admin")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)

        admin_member = MagicMock(spec=discord.Member)
        admin_member.bot = False
        admin_member.id = 601
        admin_member.guild_permissions = MagicMock()
        admin_member.guild_permissions.manage_guild = True
        admin_member.guild_permissions.administrator = False

        message = _make_channel_message(
            guild_id=int(guild_id), channel_id=6010, author=admin_member
        )
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 60100
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        # Verify the account role was written to the DB.
        async with db_session_factory() as s:
            principal = await get_or_create_platform_principal(
                s, tenant_id=tenant_id, platform="discord", external_id="601"
            )
            await s.commit()

        async with db_session_factory() as s:
            account = await get_account(s, principal.account_id)
        assert account is not None, "account must exist after turn"
        assert account.role == Role.ADMIN, (
            f"admin caller's turn must set account.role = Role.ADMIN; got: {account.role!r}"
        )

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_non_admin_turn_upserts_account_role_user(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A non-admin caller's turn sets their platform account role to user in the DB."""
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.stores.accounts import get_account
        from daimon.core.stores.domain import Role
        from daimon.core.stores.identity import get_or_create_platform_principal

        guild_id = "803000002"
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("100.00"),
        )
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_create_session.return_value = _make_fake_session("sess-role-user")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)

        regular_member = MagicMock(spec=discord.Member)
        regular_member.bot = False
        regular_member.id = 602
        regular_member.guild_permissions = MagicMock()
        regular_member.guild_permissions.manage_guild = False
        regular_member.guild_permissions.administrator = False

        message = _make_channel_message(
            guild_id=int(guild_id), channel_id=6020, author=regular_member
        )
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 60200
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        async with db_session_factory() as s:
            principal = await get_or_create_platform_principal(
                s, tenant_id=tenant_id, platform="discord", external_id="602"
            )
            await s.commit()

        async with db_session_factory() as s:
            account = await get_account(s, principal.account_id)
        assert account is not None, "account must exist after turn"
        assert account.role == Role.USER, (
            f"non-admin caller's turn must set account.role = Role.USER; got: {account.role!r}"
        )

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_role_upsert_does_not_downgrade_cli_operator_account(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A non-admin Discord turn for a platform account does NOT downgrade a
        pre-existing CLI/operator account that has role=admin (T-88-04-03).

        The role write targets only the platform-principal's account (the account
        returned by get_or_create_platform_principal). It never touches a CLI account.
        """
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.stores.accounts import get_account, set_role
        from daimon.core.stores.domain import Role
        from daimon.core.stores.identity import get_or_create_cli_principal

        guild_id = "803000003"
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("100.00"),
        )
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

        # Create a CLI principal pre-set to Role.ADMIN.
        async with db_session_factory() as s:
            cli_principal = await get_or_create_cli_principal(
                s, tenant_id=tenant_id, os_user="operator"
            )
            await set_role(s, cli_principal.account_id, Role.ADMIN)
            await s.commit()
        cli_account_id = cli_principal.account_id

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_create_session.return_value = _make_fake_session("sess-no-downgrade")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)

        # A non-admin Discord turn for a DISTINCT platform account.
        non_admin = MagicMock(spec=discord.Member)
        non_admin.bot = False
        non_admin.id = 603
        non_admin.guild_permissions = MagicMock()
        non_admin.guild_permissions.manage_guild = False
        non_admin.guild_permissions.administrator = False

        message = _make_channel_message(guild_id=int(guild_id), channel_id=6030, author=non_admin)
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 60300
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        # The CLI/operator account must still be Role.ADMIN — the Discord turn must
        # NOT have touched it.
        async with db_session_factory() as s:
            cli_account = await get_account(s, cli_account_id)
        assert cli_account is not None, "CLI account must still exist"
        assert cli_account.role == Role.ADMIN, (
            "CLI/operator admin account must NOT be downgraded by a non-admin Discord turn "
            "(the role write targets only the platform-principal's account, T-88-04-03); "
            f"got: {cli_account.role!r}"
        )

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_role_upsert_runs_with_flag_off(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The role upsert is UNCONDITIONAL — it fires even when per_caller_thread_sessions=False
        (the session-keying flag). The role write and the session-keying flag are decoupled (B4).
        """
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.stores.accounts import get_account
        from daimon.core.stores.domain import Role
        from daimon.core.stores.identity import get_or_create_platform_principal

        guild_id = "803000004"
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("100.00"),
        )
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

        mock_resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        mock_create_session.return_value = _make_fake_session("sess-role-flag-off")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        # Flag OFF — session keying falls back to legacy, but role write must still fire.
        runtime = _make_runtime(db_session_factory, per_caller_thread_sessions=False)
        bot = _make_bot(runtime)

        admin_member = MagicMock(spec=discord.Member)
        admin_member.bot = False
        admin_member.id = 604
        admin_member.guild_permissions = MagicMock()
        admin_member.guild_permissions.manage_guild = True
        admin_member.guild_permissions.administrator = False

        message = _make_channel_message(
            guild_id=int(guild_id), channel_id=6040, author=admin_member
        )
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 60400
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        async with db_session_factory() as s:
            principal = await get_or_create_platform_principal(
                s, tenant_id=tenant_id, platform="discord", external_id="604"
            )
            await s.commit()

        async with db_session_factory() as s:
            account = await get_account(s, principal.account_id)
        assert account is not None, "account must exist after turn"
        assert account.role == Role.ADMIN, (
            "role upsert must fire even when per_caller_thread_sessions=False (B4 decoupling); "
            f"got: {account.role!r}"
        )


class TestDrainLoopDeCoalescing:
    """Drain loop must partition queued mentions by author.id.

    Under per-caller sessions, coalescing distinct authors into one composite turn
    routes author B's message onto author A's session — the relocated confused-deputy
    hole on the hot path. Fix: one composite turn per author, each resolving its own
    session from message[0] of that author's slice.
    """

    async def test_drain_loop_does_not_coalesce_distinct_authors_into_one_turn(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Queued mentions from distinct authors A and B drain as TWO separate turns,
        each with that author's own message as the driving message and a single-author
        composite content — never a mixed-author '[A]: ... [B]: ...' composite in one turn.

        Approach: pre-populate _pending with [msg_a, msg_b]; call on_message with a
        fresh trigger message for the SAME thread while _handle_mention is patched to
        record calls. The drain loop fires after the first turn and must produce one
        _handle_mention call per distinct author (2 drain calls total, not 1).

        G1 closes the confused-deputy hole on the drain hot path.
        """
        from unittest.mock import patch as _patch  # noqa: PLC0415

        from daimon.core.defaults.provisioning import provision_tenant  # noqa: PLC0415

        guild_id = "804000001"
        thread_id = 8040001
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("100.00"),
        )

        runtime = _make_runtime(db_session_factory)
        bot = _make_bot(runtime)

        # Build two distinct authors with distinct author.id values.
        author_a = MagicMock()
        author_a.bot = False
        author_a.id = 701  # distinct id for A
        author_a.display_name = "AuthorA"

        author_b = MagicMock()
        author_b.bot = False
        author_b.id = 702  # distinct id for B
        author_b.display_name = "AuthorB"

        # Build queued messages: one from A, one from B, to live in _pending.
        msg_a = MagicMock(spec=discord.Message)
        msg_a.content = "hello from A"
        msg_a.author = author_a
        msg_a.add_reaction = AsyncMock()
        msg_a.attachments = []

        msg_b = MagicMock(spec=discord.Message)
        msg_b.content = "hello from B"
        msg_b.author = author_b
        msg_b.add_reaction = AsyncMock()
        msg_b.attachments = []

        # Pre-populate _pending[thread_id] before the first turn fires.
        # When the first _handle_mention call completes, the drain loop pops this.
        bot._pending[thread_id] = [msg_a, msg_b]  # pyright: ignore[reportPrivateUsage]

        # Track all _handle_mention calls: (driving_message, content_override).
        handle_calls: list[tuple[discord.Message, str | None]] = []

        async def _spy_handle_mention(
            message: discord.Message,
            guild_id_arg: str,
            tenant_id_arg: object,
            *,
            content_override: str | None = None,
            created_thread_ids: list[int] | None = None,
            attachments_override: list[discord.Attachment] | None = None,
        ) -> None:
            # Record the call then return immediately (no DB work needed).
            handle_calls.append((message, content_override))

        trigger_msg = _make_thread_message_for_bot(
            guild_id=int(guild_id),
            thread_id=thread_id,
            author_id=703,  # a third distinct author for the trigger
        )

        with _patch.object(bot, "_handle_mention", side_effect=_spy_handle_mention):
            await bot.on_message(trigger_msg)

        # Total calls: 1 (trigger turn) + N drain turns.
        # With the CURRENT (unfixed) code the drain produces 1 call (mixed-author composite).
        # After the fix it must produce 2 drain calls (one per author).
        # Total expected = 1 trigger + 2 drain = 3.
        drain_calls = handle_calls[1:]  # skip the first (trigger) call
        assert len(drain_calls) == 2, (
            f"drain loop must produce exactly 2 _handle_mention calls (one per distinct author); "
            f"got {len(drain_calls)} drain calls (total calls={len(handle_calls)}). "
            "G1: coalescing B's message onto A's session is the confused-deputy hole on the hot path."
        )

        # First drain call must drive author A's message with A-only content.
        first_drain_msg, first_override = drain_calls[0]
        assert first_drain_msg.author.id == 701, (
            f"first drain turn must be driven by author A's message (id=701); "
            f"got author.id={first_drain_msg.author.id}"
        )
        assert "[AuthorB]" not in (first_override or ""), (
            "first drain turn content must not contain author B's prefix — "
            f"it would be a mixed-author composite; got: {first_override!r}"
        )

        # Second drain call must drive author B's message with B-only content.
        second_drain_msg, second_override = drain_calls[1]
        assert second_drain_msg.author.id == 702, (
            f"second drain turn must be driven by author B's message (id=702); "
            f"got author.id={second_drain_msg.author.id}"
        )
        assert "[AuthorA]" not in (second_override or ""), (
            "second drain turn content must not contain author A's prefix — "
            f"it would be a mixed-author composite; got: {second_override!r}"
        )
