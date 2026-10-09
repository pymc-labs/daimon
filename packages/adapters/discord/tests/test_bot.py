"""Tests for DaimonBot in-flight concurrency cap, is_admin derivation,
per-caller session keying, and per-turn role upsert.

Plan 50-08: per-tenant in-flight counter + is_admin derivation.
Plan 88-04: per-(thread,account) session keying (flag-gated) + unconditional role upsert.
"""

from __future__ import annotations

import uuid
from contextlib import suppress
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import BillingSettings, McpSettings, ThreadNamingSettings
from daimon.core.errors import DaimonError
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores.tenants import set_turn_cap
from daimon.core.turn.deps import build_turn_deps
from daimon.testing import ma_agent, ma_environment, ma_session
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .harness import make_bot


def _make_runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    max_concurrent_turns_per_tenant: int = 3,
) -> DiscordRuntime:
    settings = MagicMock()
    settings.agent_identity.enabled = True
    settings.mcp = McpSettings()
    settings.defaults_root = MagicMock()
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = max_concurrent_turns_per_tenant
    discord_settings.thread_open_notice_after_s = 3.0
    settings.discord = discord_settings
    settings.thread_naming = ThreadNamingSettings(enabled=False)
    anthropic = AsyncMock()
    # Dead-session recovery's transcript rescue (`_replay_previous_session`)
    # walks this as an async iterator, not an awaitable -- an unconfigured
    # AsyncMock attribute returns a coroutine instead and blows up with
    # "'async for' requires an object with __aiter__ method". Empty history
    # degrades it to the history-only rung, same as before that rescue path
    # existed.
    anthropic.beta.sessions.events.list = MagicMock(return_value=_AsyncIter([]))
    # A live agent/environment by default -- admit() now reads archived_at off
    # the retrieved agent, so an unconfigured AsyncMock (whose attributes are
    # themselves truthy mocks) would wrongly look archived on every turn.
    anthropic.beta.agents.retrieve = AsyncMock(return_value=ma_agent())
    anthropic.beta.environments.retrieve = AsyncMock(return_value=ma_environment())
    resolver_cache = new_resolver_cache()
    deployment_default = DeploymentDefault()
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        turn_deps=build_turn_deps(
            settings,
            anthropic,
            sessionmaker,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            billing_config=None,
        ),
    )


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


@pytest.mark.parametrize(
    (
        "source",
        "resolved_author_id",
        "resolved_webhook_id",
        "resolved_application_id",
        "qa_bot_author",
        "qa_bot_listed",
        "expected",
    ),
    [
        ("tool", 999, None, None, False, False, True),
        ("auto_thread", 999, None, None, False, False, False),
        ("tool", 777, None, None, False, False, False),
        ("tool", 777, 900, 10, False, False, True),
        ("tool", 777, 901, 11, False, False, False),
        ("tool", 777, 900, 10, True, True, True),
        ("tool", 777, 900, 10, True, False, False),
    ],
)
async def test_reply_to_recorded_agent_post_starts_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
    source: Literal["tool", "auto_thread"],
    resolved_author_id: int,
    resolved_webhook_id: int | None,
    resolved_application_id: int | None,
    qa_bot_author: bool,
    qa_bot_listed: bool,
    expected: bool,
) -> None:
    from daimon.core.defaults.provisioning import provision_tenant
    from daimon.core.stores.agent_posts import record_post

    guild_id = "801000098"
    result = await provision_tenant(
        db_session_factory,
        platform="discord",
        workspace_id=guild_id,
        signup_credit=Decimal("5.00"),
    )
    async with db_session_factory() as session, session.begin():
        await record_post(
            session,
            tenant_id=result.tenant_id,
            platform="discord",
            channel_id="789",
            message_id="123",
            agent_id=uuid.uuid4(),
            source=source,
        )
    runtime = _make_runtime(db_session_factory)
    runtime.settings.discord.qa_bot_user_ids = ("333",) if qa_bot_listed else ()
    bot = make_bot(runtime)
    bot._connection.application_id = 10  # pyright: ignore[reportPrivateUsage]
    bot._handle_mention = AsyncMock()  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportMethodAssign]
    message = _make_channel_message(guild_id=int(guild_id), author_id=333 if qa_bot_author else 111)
    message.author.bot = qa_bot_author
    message.mentions = []
    message.webhook_id = None
    message.reference = discord.MessageReference(
        message_id=123, channel_id=789, guild_id=int(guild_id)
    )
    resolved = MagicMock(spec=discord.Message)
    resolved.author.id = resolved_author_id
    resolved.webhook_id = resolved_webhook_id
    resolved.application_id = resolved_application_id
    message.reference.resolved = resolved
    await bot.on_message(message)
    if expected:
        bot._handle_mention.assert_awaited_once()  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue]
    else:
        bot._handle_mention.assert_not_awaited()  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue]


@pytest.mark.parametrize("excluded", [False, True])
async def test_identity_off_or_excluded_ignores_reply_without_mention(
    db_session_factory: async_sessionmaker[AsyncSession],
    excluded: bool,
) -> None:
    runtime = _make_runtime(db_session_factory)
    if excluded:
        runtime.settings.agent_identity.excluded_discord_guild_ids = ["801000099"]
    else:
        runtime.settings.agent_identity.enabled = False
    bot = make_bot(runtime)
    bot._handle_mention = AsyncMock()  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportMethodAssign]
    message = _make_channel_message(guild_id=801000099)
    message.mentions = []
    message.webhook_id = None
    message.reference = discord.MessageReference(message_id=123, channel_id=789, guild_id=801000099)
    resolved = MagicMock(spec=discord.Message)
    resolved.author.id = bot.user.id
    resolved.webhook_id = None
    message.reference.resolved = resolved
    with patch("daimon.adapters.discord.bot.get_post", new_callable=AsyncMock) as get_post:
        await bot.on_message(message)
    get_post.assert_not_awaited()
    bot._handle_mention.assert_not_awaited()  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue]


async def test_reply_to_unrecorded_post_does_not_start_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bot = make_bot(_make_runtime(db_session_factory))
    bot._handle_mention = AsyncMock()  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportMethodAssign]
    message = _make_channel_message(guild_id=801000097)
    message.mentions = []
    message.webhook_id = None
    message.reference = discord.MessageReference(message_id=123, channel_id=789, guild_id=801000097)
    resolved = MagicMock(spec=discord.Message)
    resolved.author.id = bot.user.id
    resolved.webhook_id = None
    message.reference.resolved = resolved
    await bot.on_message(message)
    bot._handle_mention.assert_not_awaited()  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue]


async def test_reply_lookup_failure_does_not_start_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bot = make_bot(_make_runtime(db_session_factory))
    bot._handle_mention = AsyncMock()  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportMethodAssign]
    message = _make_channel_message(guild_id=801000096)
    message.mentions = []
    message.webhook_id = None
    message.reference = discord.MessageReference(message_id=123, channel_id=789, guild_id=801000096)
    resolved = MagicMock(spec=discord.Message)
    resolved.author.id = bot.user.id
    resolved.webhook_id = None
    message.reference.resolved = resolved
    with patch("daimon.adapters.discord.bot.get_post", new_callable=AsyncMock) as get_post:
        get_post.side_effect = RuntimeError("database unavailable")
        await bot.on_message(message)
    bot._handle_mention.assert_not_awaited()  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue]


async def test_mention_skips_reply_lookup(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bot = make_bot(_make_runtime(db_session_factory))
    bot._handle_mention = AsyncMock()  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportMethodAssign]
    message = _make_channel_message(guild_id=801000095)
    message.webhook_id = None
    message.reference = discord.MessageReference(message_id=123, channel_id=789, guild_id=801000095)
    resolved = MagicMock(spec=discord.Message)
    resolved.author.id = bot.user.id
    resolved.webhook_id = None
    message.reference.resolved = resolved
    with patch("daimon.adapters.discord.bot.get_post", new_callable=AsyncMock) as get_post:
        await bot.on_message(message)
    get_post.assert_not_awaited()


class TestInflightCapRejection:
    """4th turn for a saturated tenant rejected (SCALE-01)."""

    async def test_tenant_override_admits_above_deployment_default(
        self, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from daimon.core.defaults.provisioning import provision_tenant

        guild_id = "801000099"
        result = await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("5.00"),
        )
        async with db_session_factory() as session, session.begin():
            await set_turn_cap(session, tenant_id=result.tenant_id, cap=30)
        bot = make_bot(_make_runtime(db_session_factory, max_concurrent_turns_per_tenant=3))
        for _ in range(3):
            bot.turn_queue.claim(result.tenant_id)
        bot._handle_mention = AsyncMock()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue, reportMethodAssign]

        message = _make_channel_message(guild_id=int(guild_id))
        await bot.on_message(message)

        bot._handle_mention.assert_awaited_once()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
        assert bot.turn_queue.in_flight(result.tenant_id) == 3
        assert bot.turn_queue.depth() == 0, "the raised cap admits at once, no queueing"

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_on_message_refuses_when_the_tenant_queue_is_full(
        self,
        mock_resolve_config: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A turn for a saturated tenant whose queue is full is refused (SCALE-01).

        Over the cap a turn queues; the plain refusal is only the last resort,
        when the queue is full too. Pre-seed the slots at the cap with no
        queue room; the next on_message must send the over-cap message and
        NOT start a turn.
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
        bot = make_bot(runtime)

        # Saturate the tenant's slots and leave no queue room.
        bot.turn_queue.max_queued_per_tenant = 0
        for _ in range(cap):
            bot.turn_queue.claim(tenant_id)

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

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-dec")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)
        message = _make_channel_message(guild_id=int(guild_id))
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 8001
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        # After the turn completes, the tenant holds no slot.
        count_after = bot.turn_queue.in_flight(tenant_id)
        assert count_after == 0, (
            f"in-flight counter must be 0 after a successful turn; got {count_after}"
        )

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-dec-err")
        mock_run_turn.side_effect = _anthropic.APIConnectionError(request=MagicMock())
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)
        message = _make_channel_message(guild_id=int(guild_id))
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 8002
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        count_after = bot.turn_queue.in_flight(tenant_id)
        assert count_after == 0, (
            f"in-flight counter must be 0 after a failed turn; got {count_after}"
        )


class TestInflightIsolation:
    """Per-tenant isolation: A saturated, B unaffected."""

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-isolation")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory, max_concurrent_turns_per_tenant=cap)
        bot = make_bot(runtime)

        # Saturate only guild A, with no queue room.
        bot.turn_queue.max_queued_per_tenant = 0
        for _ in range(cap):
            bot.turn_queue.claim(tenant_a)

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

    is_admin is not threaded into the vault credential (identity-stable);
    instead the live DB account.role is written each turn — the MCP gate then
    reads it live.

    These tests verify that manage_guild derives correctly.
    """

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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

        The MCP gate reads account.role on every request (88-03).
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
        mock_create_session.return_value = ma_session(id="sess-admin-true")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)

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

        # is_admin drives a live DB role write, not vault-credential baking.
        mock_create_session.assert_called_once()

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

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-admin-false")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)

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

        mock_create_session.assert_called_once()

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

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-user-admin")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)

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
        bot = make_bot(runtime)

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
        bot = make_bot(runtime)

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


class TestOnReadySweepWidenedReconcile:
    """The re-seed loop reconciles every registered, joined tenant on boot, not
    just tenants stuck in pending/failed (D-06)."""

    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    async def test_boot_sweep_reconciles_a_tenant_that_is_already_ready(
        self,
        mock_reconcile: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        db_engine: AsyncEngine,
    ) -> None:
        """A tenant already in status='ready' still gets `_seed_tenant_defaults`
        invoked on boot -- the whole point of widening the sweep."""
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.defaults.report import ApplyReport
        from daimon.core.ma_identity import derive_tenant_uuid

        guild_id = 900000010
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=str(guild_id),
            signup_credit=Decimal("0"),
        )
        # provision_tenant leaves provision_status at its server default, "ready".
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(guild_id))
        mock_reconcile.return_value = ApplyReport()

        # The sweep spawns one background task per guild, and each opens its own
        # session: give them a sessionmaker on the engine, not `db_session_factory`
        # (bound to one connection), so the concurrent tasks don't share a transaction.
        runtime = _make_runtime(async_sessionmaker(bind=db_engine, expire_on_commit=False))
        bot = make_bot(runtime)
        bot._connection._guilds = {guild_id: _make_sweep_guild(guild_id)}  # pyright: ignore[reportPrivateUsage]
        bot.tree.clear_commands = MagicMock()  # type: ignore[method-assign]
        bot.tree.sync = AsyncMock()  # type: ignore[method-assign]

        await bot.on_ready()
        while bot._bg_tasks:  # pyright: ignore[reportPrivateUsage]
            import asyncio as _asyncio

            await _asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]

        mock_reconcile.assert_awaited_once()
        assert mock_reconcile.await_args is not None
        assert mock_reconcile.await_args.kwargs["tenant_id"] == tenant_id, (
            "an already-ready tenant must still be reconciled against the shipped "
            "defaults on boot (D-06)"
        )

    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    async def test_boot_sweep_still_reconciles_pending_and_failed_tenants(
        self,
        mock_reconcile: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        db_engine: AsyncEngine,
    ) -> None:
        """Pre-existing behavior is not lost: tenants stuck in pending or failed
        are still reconciled on boot."""
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.defaults.report import ApplyReport
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.stores.tenants import set_provision_status

        guild_pending = 900000011
        guild_failed = 900000012
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=str(guild_pending),
            signup_credit=Decimal("0"),
        )
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=str(guild_failed),
            signup_credit=Decimal("0"),
        )
        tenant_pending = derive_tenant_uuid(platform="discord", workspace_id=str(guild_pending))
        tenant_failed = derive_tenant_uuid(platform="discord", workspace_id=str(guild_failed))
        await set_provision_status(db_session_factory, tenant_id=tenant_pending, status="pending")
        await set_provision_status(db_session_factory, tenant_id=tenant_failed, status="failed")
        mock_reconcile.return_value = ApplyReport()

        # The sweep spawns one background task per guild, and each opens its own
        # session: give them a sessionmaker on the engine, not `db_session_factory`
        # (bound to one connection), so the concurrent tasks don't share a transaction.
        runtime = _make_runtime(async_sessionmaker(bind=db_engine, expire_on_commit=False))
        bot = make_bot(runtime)
        bot._connection._guilds = {  # pyright: ignore[reportPrivateUsage]
            guild_pending: _make_sweep_guild(guild_pending),
            guild_failed: _make_sweep_guild(guild_failed),
        }
        bot.tree.clear_commands = MagicMock()  # type: ignore[method-assign]
        bot.tree.sync = AsyncMock()  # type: ignore[method-assign]

        await bot.on_ready()
        while bot._bg_tasks:  # pyright: ignore[reportPrivateUsage]
            import asyncio as _asyncio

            await _asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]

        reconciled_tenant_ids = {c.kwargs["tenant_id"] for c in mock_reconcile.await_args_list}
        assert tenant_pending in reconciled_tenant_ids, (
            "a pending tenant must be reconciled on boot"
        )
        assert tenant_failed in reconciled_tenant_ids, "a failed tenant must be reconciled on boot"

    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    async def test_boot_sweep_skips_a_registered_tenant_whose_guild_is_not_joined(
        self,
        mock_reconcile: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A registered tenant whose guild the bot has not (re)joined is never
        reconciled -- the not-joined guard still holds."""
        from daimon.core.defaults.provisioning import provision_tenant

        guild_id = 900000013
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=str(guild_id),
            signup_credit=Decimal("0"),
        )

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)
        # No guild registered in bot._connection._guilds -- the bot has not joined it.
        bot.tree.clear_commands = MagicMock()  # type: ignore[method-assign]
        bot.tree.sync = AsyncMock()  # type: ignore[method-assign]

        await bot.on_ready()
        while bot._bg_tasks:  # pyright: ignore[reportPrivateUsage]
            import asyncio as _asyncio

            await _asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]

        mock_reconcile.assert_not_awaited()


class TestReadyEmbedSuppression:
    """The ready embed is suppressed on the boot sweep when a reconcile changes
    nothing for an already-ready tenant (D-23); a real propagation or a failure
    always announces itself."""

    @patch("daimon.adapters.discord.bot.set_provision_status", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    async def test_boot_sweep_posts_no_embed_when_a_ready_tenant_had_nothing_to_change(
        self,
        mock_reconcile: AsyncMock,
        mock_set_provision_status: AsyncMock,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """was_ready=True + an all-skipped report must post exactly zero messages."""
        from daimon.core.defaults.report import ApplyReport

        mock_reconcile.return_value = ApplyReport()

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)
        guild = _make_sweep_guild(900000020)

        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=uuid.uuid4(), guild=guild, was_ready=True
        )

        assert guild.system_channel.send.await_count == 0, (  # pyright: ignore[reportUnknownMemberType]
            "an already-ready tenant with an all-skipped report must post zero messages"
        )

    @patch("daimon.adapters.discord.bot.set_provision_status", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    async def test_boot_sweep_posts_no_embed_when_a_ready_tenant_changed(
        self,
        mock_reconcile: AsyncMock,
        mock_set_provision_status: AsyncMock,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """was_ready=True and the report records a real change: still zero messages.
        Every deploy touching defaults/ reconciles every guild, so posting on
        change means posting in every channel on every deploy."""
        from daimon.core.defaults.report import Action, ApplyReport, ResourceOutcome

        report = ApplyReport()
        report.add(ResourceOutcome(kind="agent", name="test-agent", action=Action.UPDATED))
        mock_reconcile.return_value = report

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)
        guild = _make_sweep_guild(900000021)

        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=uuid.uuid4(), guild=guild, was_ready=True
        )

        assert guild.system_channel.send.await_count == 0, (  # pyright: ignore[reportUnknownMemberType]
            "an already-ready tenant must stay silent even when the reconcile "
            "changed something -- the ready embed is an install confirmation, "
            "not a deploy notification"
        )

    @patch("daimon.adapters.discord.bot.set_provision_status", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    async def test_a_pending_tenant_still_gets_its_embed_on_an_all_skipped_report(
        self,
        mock_reconcile: AsyncMock,
        mock_set_provision_status: AsyncMock,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """was_ready=False (first-run/recovery) is never suppressed, even on an
        all-skipped report."""
        from daimon.core.defaults.report import ApplyReport

        mock_reconcile.return_value = ApplyReport()

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)
        guild = _make_sweep_guild(900000022)

        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=uuid.uuid4(), guild=guild, was_ready=False
        )

        guild.system_channel.send.assert_awaited_once()  # pyright: ignore[reportUnknownMemberType]
        embed = guild.system_channel.send.await_args.kwargs["embed"]  # pyright: ignore[reportUnknownMemberType]
        assert "ready" in (embed.title or "").lower(), (
            "a first-run/recovery seed (was_ready=False) must not be suppressed "
            "even on an all-skipped report"
        )

    @patch("daimon.adapters.discord.bot.set_provision_status", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    async def test_a_failed_reconcile_posts_no_embed_when_the_tenant_was_already_ready(
        self,
        mock_reconcile: AsyncMock,
        mock_set_provision_status: AsyncMock,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """was_ready=True: a failed reconcile must NOT post the snag embed or demote
        the tenant -- a transient failure on an already-serving install stays quiet
        so a working guild's turns are never taken offline."""
        from daimon.core.defaults.report import Action, ApplyReport, ResourceOutcome

        report = ApplyReport()
        report.add(
            ResourceOutcome(kind="skill", name="broken-skill", action=Action.FAILED, error="boom")
        )
        mock_reconcile.return_value = report

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)
        guild = _make_sweep_guild(900000023)

        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=uuid.uuid4(), guild=guild, was_ready=True
        )

        guild.system_channel.send.assert_not_awaited()  # pyright: ignore[reportUnknownMemberType]

    @patch("daimon.adapters.discord.bot.set_provision_status", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    async def test_a_raising_reconcile_posts_no_embed_when_the_tenant_was_already_ready(
        self,
        mock_reconcile: AsyncMock,
        mock_set_provision_status: AsyncMock,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """was_ready=True and the reconcile RAISES: still zero messages. The boot
        sweep reconciles every guild, so one provider error would otherwise put a
        snag embed in every channel of every install at once."""
        mock_reconcile.side_effect = DaimonError("provider blew up")

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)
        guild = _make_sweep_guild(900000025)

        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=uuid.uuid4(), guild=guild, was_ready=True
        )

        guild.system_channel.send.assert_not_awaited()  # pyright: ignore[reportUnknownMemberType]

    @patch("daimon.adapters.discord.bot.set_provision_status", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    async def test_a_raising_reconcile_still_posts_the_snag_embed_when_not_previously_ready(
        self,
        mock_reconcile: AsyncMock,
        mock_set_provision_status: AsyncMock,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """was_ready=False: a raising reconcile still announces itself, so a first
        install that genuinely broke is never left silent."""
        mock_reconcile.side_effect = DaimonError("provider blew up")

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)
        guild = _make_sweep_guild(900000026)

        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=uuid.uuid4(), guild=guild, was_ready=False
        )

        guild.system_channel.send.assert_awaited_once()  # pyright: ignore[reportUnknownMemberType]

    @patch("daimon.adapters.discord.bot.set_provision_status", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    async def test_a_failed_reconcile_still_posts_the_snag_embed_when_not_previously_ready(
        self,
        mock_reconcile: AsyncMock,
        mock_set_provision_status: AsyncMock,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """was_ready=False (first-run/recovery): a failed reconcile still announces
        itself with the snag embed, unchanged from prior behavior."""
        from daimon.core.defaults.report import Action, ApplyReport, ResourceOutcome

        report = ApplyReport()
        report.add(
            ResourceOutcome(kind="skill", name="broken-skill", action=Action.FAILED, error="boom")
        )
        mock_reconcile.return_value = report

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)
        guild = _make_sweep_guild(900000024)

        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=uuid.uuid4(), guild=guild, was_ready=False
        )

        guild.system_channel.send.assert_awaited_once()  # pyright: ignore[reportUnknownMemberType]
        embed = guild.system_channel.send.await_args.kwargs["embed"]  # pyright: ignore[reportUnknownMemberType]
        assert "snag" in (embed.title or "").lower(), (
            "a not-previously-ready failure must announce itself"
        )


class TestReconcileFailureReasonPersistence:
    """A failed boot reconcile records why on the tenant row, and a later
    success clears it (D-08)."""

    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    async def test_a_failed_reconcile_records_the_reason_without_demoting_a_ready_tenant(
        self,
        mock_reconcile: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A resource-level failure in the report is composed into a reason
        naming the failing resource's kind and name, and persisted -- but a
        tenant that was already ready (was_ready=True, the boot-sweep case)
        must stay ready rather than being demoted to failed."""
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.defaults.report import Action, ApplyReport, ResourceOutcome
        from daimon.core.stores.tenants import get_tenant_liveness

        guild_id = "900000030"
        result = await provision_tenant(
            db_session_factory, platform="discord", workspace_id=guild_id
        )
        tenant_id = result.tenant_id

        report = ApplyReport()
        report.add(
            ResourceOutcome(
                kind="skill", name="broken-skill", action=Action.FAILED, error="upload 503"
            )
        )
        mock_reconcile.return_value = report

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)
        guild = _make_sweep_guild(int(guild_id))

        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=tenant_id, guild=guild, was_ready=True
        )

        tr = await get_tenant_liveness(db_session_factory, tenant_id)
        assert tr is not None
        assert tr.provision_status == "ready", (
            "a previously-ready tenant must not be demoted by a transient reconcile failure"
        )
        assert tr.last_reconcile_error is not None
        assert "skill" in tr.last_reconcile_error, "reason must name the failing resource kind"
        assert "broken-skill" in tr.last_reconcile_error, "reason must name the failing resource"

    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    async def test_a_successful_reconcile_clears_a_previously_recorded_reason(
        self,
        mock_reconcile: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A tenant with a stale failed status and reason from a prior boot is
        cleared once a subsequent reconcile succeeds."""
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.defaults.report import ApplyReport
        from daimon.core.stores.tenants import get_tenant_liveness, set_provision_status

        guild_id = "900000031"
        result = await provision_tenant(
            db_session_factory, platform="discord", workspace_id=guild_id
        )
        tenant_id = result.tenant_id
        await set_provision_status(
            db_session_factory, tenant_id=tenant_id, status="failed", reason="stale failure"
        )
        mock_reconcile.return_value = ApplyReport()

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)
        guild = _make_sweep_guild(int(guild_id))

        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=tenant_id, guild=guild, was_ready=False
        )

        tr = await get_tenant_liveness(db_session_factory, tenant_id)
        assert tr is not None
        assert tr.provision_status == "ready"
        assert tr.last_reconcile_error is None, (
            "a successful reconcile must clear a previously recorded reason"
        )

    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    async def test_an_unexpected_exception_records_only_the_exception_type(
        self,
        mock_reconcile: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The unexpected-error branch records the exception TYPE only -- never
        its message body, which may carry request/response content."""
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.stores.tenants import get_tenant_liveness

        guild_id = "900000032"
        result = await provision_tenant(
            db_session_factory, platform="discord", workspace_id=guild_id
        )
        tenant_id = result.tenant_id

        secret_message = "leaked-credential-abc123 in request body"
        mock_reconcile.side_effect = ValueError(secret_message)

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)
        guild = _make_sweep_guild(int(guild_id))

        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=tenant_id, guild=guild, was_ready=False
        )

        tr = await get_tenant_liveness(db_session_factory, tenant_id)
        assert tr is not None
        assert tr.provision_status == "failed"
        assert tr.last_reconcile_error is not None
        assert "ValueError" in tr.last_reconcile_error, "reason must name the exception type"
        assert secret_message not in tr.last_reconcile_error, (
            "the exception's message body must never be recorded for an unexpected error"
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
    """Per-(thread,account) session keying: get_live_thread_session and both
    create_thread_session calls use session_account_id=principal.account_id, so
    distinct callers get distinct sessions.
    """

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-admin-starter")
        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)

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
        mock_create_session.return_value = ma_session(id="sess-lowpriv-caller")
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

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        import anthropic as _anthropic
        from daimon.core.errors import TurnError

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
            ma_session(id="sess-dead-original"),
            ma_session(id="sess-recreated"),
        ]

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)

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


class TestPerTurnRoleUpsert:
    """Per-turn unconditional account.role upsert from Discord admin perms.

    The role write runs BEFORE run_turn.
    It targets only the platform-principal's account — never CLI/operator accounts (T-88-04-03).
    """

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-role-admin")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)

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

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-role-user")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)

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

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-no-downgrade")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)

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
        from unittest.mock import patch as _patch

        from daimon.core.defaults.provisioning import provision_tenant

        guild_id = "804000001"
        thread_id = 8040001
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("100.00"),
        )

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)

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


class TestCredentialButtonRegistration:
    """setup_hook registers CredentialRequestButton as a dynamic item exactly once."""

    async def test_setup_hook_registers_credential_request_button(
        self, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from daimon.adapters.discord.credential_button import CredentialRequestButton

        runtime = _make_runtime(db_session_factory)
        bot = make_bot(runtime)
        bot.start_orphan_recovery = MagicMock()  # type: ignore[method-assign]  # the boot sweep is not under test

        await bot.setup_hook()

        # The registry is private (ConnectionState._view_store._dynamic_items) --
        # discord.py exposes no public read of the registration.
        dynamic_items = (
            bot._connection._view_store._dynamic_items  # pyright: ignore[reportPrivateUsage]  # discord.py exposes no public accessor
        )
        template = CredentialRequestButton.__discord_ui_compiled_template__
        assert dynamic_items.get(template) is CredentialRequestButton, (
            "CredentialRequestButton's compiled template must be registered as a "
            "dynamic item after setup_hook runs"
        )


class TestGuildInstallLifecycle:
    async def test_delayed_remove_cannot_archive_after_rejoin(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A remove DB write delayed across a join must not leave the tenant archived."""
        import asyncio

        from daimon.adapters.discord import bot as bot_module
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.stores.tenants import get_tenant, set_provision_status

        guild_id = 900000111
        guild = MagicMock(spec=discord.Guild)
        guild.id = guild_id
        guild.name = "Rejoined Guild"
        runtime = _make_runtime(db_session_factory)
        runtime.settings.billing = BillingSettings(signup_credit=Decimal("0"))
        bot = make_bot(runtime)
        await provision_tenant(db_session_factory, platform="discord", workspace_id=str(guild_id))

        remove_entered = asyncio.Event()
        release_remove = asyncio.Event()
        join_unarchived = asyncio.Event()

        async def delayed_status(
            session_factory: async_sessionmaker[AsyncSession],
            *,
            tenant_id: uuid.UUID,
            status: str | None = None,
            archive: bool = False,
            clear_archive: bool = False,
            reason: str | None = None,
            clear_reason: bool = False,
        ) -> None:
            if archive:
                remove_entered.set()
                await release_remove.wait()
            await set_provision_status(
                session_factory,
                tenant_id=tenant_id,
                status=status,
                archive=archive,
                clear_archive=clear_archive,
                reason=reason,
                clear_reason=clear_reason,
            )
            if clear_archive:
                join_unarchived.set()

        with (
            patch.object(bot_module, "set_provision_status", delayed_status),
            patch.object(bot, "_post_to_guild", new_callable=AsyncMock),
            patch.object(bot, "_seed_tenant_defaults", new_callable=AsyncMock),
            patch.object(bot.tree, "sync", new_callable=AsyncMock),
        ):
            # discord.py removes the old guild from its cache before dispatching
            # guild_remove, so the old callback enters its archive write now.
            remove_task = asyncio.create_task(bot.on_guild_remove(guild))
            await remove_entered.wait()

            # parse_guild_create adds the rejoined guild before dispatching join.
            bot._connection._guilds[guild_id] = guild  # pyright: ignore[reportPrivateUsage]
            join_task = asyncio.create_task(bot.on_guild_join(guild))
            # Let an unguarded join finish its unarchive write while the older
            # remove write is still paused. With the lifecycle lock, join waits
            # until remove is released, so the bounded wait expires instead.
            with suppress(TimeoutError):
                await asyncio.wait_for(join_unarchived.wait(), timeout=2.0)
            release_remove.set()
            await asyncio.gather(remove_task, join_task)
            for task in tuple(bot._bg_tasks):  # pyright: ignore[reportPrivateUsage]
                await task

        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(guild_id))
        async with db_session_factory() as session:
            tenant = await get_tenant(session, tenant_id)
        assert tenant is not None and tenant.archived_at is None, (
            "a completed rejoin must remain live after an earlier remove callback"
        )

    async def test_on_ready_revives_archived_known_guild_without_welcome_or_credit(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from daimon.core.defaults.provisioning import provision_tenant
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.stores.tenant_ledger import list_for_tenant
        from daimon.core.stores.tenants import get_tenant, set_provision_status

        guild_id = "900000112"
        provisioned = await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id=guild_id,
            signup_credit=Decimal("1.00"),
        )
        await set_provision_status(
            db_session_factory, tenant_id=provisioned.tenant_id, archive=True
        )

        runtime = _make_runtime(db_session_factory)
        runtime.settings.billing = BillingSettings(signup_credit=Decimal("1.00"))
        bot = make_bot(runtime)
        guild = _make_sweep_guild(int(guild_id))
        bot._connection._guilds[int(guild_id)] = guild  # pyright: ignore[reportPrivateUsage]

        with (
            patch.object(bot, "_post_to_guild", new_callable=AsyncMock) as post,
            patch.object(bot, "_seed_tenant_defaults", new_callable=AsyncMock),
            patch.object(bot.tree, "sync", new_callable=AsyncMock) as sync,
            patch.object(bot.tree, "clear_commands", new_callable=MagicMock) as clear_commands,
        ):
            await bot.on_ready()
            for task in tuple(bot._bg_tasks):  # pyright: ignore[reportPrivateUsage]
                await task

        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
        async with db_session_factory() as session:
            tenant = await get_tenant(session, tenant_id)
            ledger = await list_for_tenant(session, tenant_id=tenant_id)
        assert tenant is not None and tenant.archived_at is None, (
            "on_ready must revive a known tenant when its guild is in the cache"
        )
        assert tenant.provision_status == "pending", "the recovered tenant must be reseeded"
        assert len(ledger) == 1, "rejoin must not issue a second signup credit"
        post.assert_not_awaited()
        assert sync.await_count == 2, "recovery must preserve guild and global command sync"
        clear_commands.assert_called_once(), "recovery must still clear guild-scoped commands"


async def test_orchestrate_boundary_persists_one_terminal_outcome(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
) -> None:
    from daimon.core.stores.turn_outcomes import list_for_tenant
    from daimon.core.turn.outcomes import current_outcome, drain_outcomes
    from daimon.core.turn.termination import TerminationReason
    from daimon.testing.factories import make_tenant

    tenant = await make_tenant(db_session)
    await db_session.commit()
    sm = async_sessionmaker(db_engine)
    bot = make_bot(_make_runtime(sm))

    async def pipeline(*args, **kwargs):
        observation = current_outcome.get()
        assert observation is not None
        observation.finish(reason=TerminationReason.COMPLETED)

    message = MagicMock(spec=discord.Message)
    message.channel.id = 123
    with patch.object(bot, "_orchestrate_observed", side_effect=pipeline):
        await bot._orchestrate(message, "guild", tenant.id)
    await drain_outcomes()
    async with sm() as session:
        rows = await list_for_tenant(session, tenant.id)
    assert len(rows) == 1
    assert rows[0].reason == TerminationReason.COMPLETED
    assert rows[0].platform == "discord" and rows[0].channel_id == "123"
