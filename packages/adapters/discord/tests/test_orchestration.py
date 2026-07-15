"""Tests for on_message turn orchestration in DaimonBot."""

from __future__ import annotations

import types
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic as _anthropic
import discord
import pytest
import structlog.testing
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent, BetaManagedAgentsSession
from anthropic.types.beta.beta_cloud_config import BetaCloudConfig
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_model_config import (
    BetaManagedAgentsModelConfig as _AgentModelConfig,
)
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage
from anthropic.types.beta.beta_packages import BetaPackages
from anthropic.types.beta.beta_unrestricted_network import BetaUnrestrictedNetwork
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import McpSettings
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault, ResolvedConfig, ScopeContext
from daimon.core.stores import tenant_ledger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import make_tenant


def _make_fake_session(session_id: str = "sess_test") -> BetaManagedAgentsSession:
    """Construct a real BetaManagedAgentsSession with validated fields."""
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
    tenant_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> DiscordRuntime:
    """Build a DiscordRuntime with a mock anthropic client.

    `runtime.anthropic.beta.agents.retrieve` / `.environments.retrieve` are
    pre-wired to return validated SDK objects so the resolver-id → re-retrieve
    pattern in `bot._orchestrate` returns realistic BetaManagedAgentsAgent /
    BetaEnvironment instances downstream.
    """
    _ = tenant_id  # runtime no longer carries tenant_id (D-06); bot.py threads it (Plan 04)
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.billing.markup = Decimal("1.0")
    settings.billing.signup_credit = Decimal("0")
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 100  # effectively uncapped in tests
    settings.discord = discord_settings
    anthropic = AsyncMock()
    anthropic.beta.agents.retrieve = AsyncMock(return_value=_make_fake_agent())
    anthropic.beta.environments.retrieve = AsyncMock(return_value=_make_fake_environment())
    from daimon.core.ma_resolver import new_resolver_cache  # noqa: PLC0415

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
    """Build a DaimonBot with minimal intents."""
    intents = discord.Intents.default()
    intents.message_content = True
    bot = DaimonBot(runtime=runtime, intents=intents)
    # Set bot user so should_process_message passes
    bot._connection.user = MagicMock(spec=discord.ClientUser)  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.id = 999  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.mentioned_in = MagicMock(return_value=True)  # pyright: ignore[reportPrivateUsage]
    return bot


class _AsyncIter:
    """Async iterator adapter for mocked channel/thread history."""

    def __init__(self, items: list[discord.Message]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> discord.Message:
        try:
            return next(self._items)
        except StopIteration as err:
            raise StopAsyncIteration from err


def _make_channel_message(
    *,
    content: str = "<@999> hello",
    guild_id: int = 123456,
    channel_id: int = 789,
    author_id: int = 111,
) -> discord.Message:
    """Mock a message in a regular text channel (not a thread)."""
    message = MagicMock(spec=discord.Message)
    message.content = content
    message.author = MagicMock()
    message.author.bot = False
    message.author.id = author_id
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = guild_id
    # Channel is a regular text channel (not a Thread)
    message.channel = MagicMock()
    # Make isinstance(message.channel, discord.Thread) return False
    # and isinstance(message.channel, discord.TextChannel) return True
    message.channel.__class__ = discord.TextChannel
    message.channel.id = channel_id
    message.channel.send = AsyncMock()
    # Stub channel.history so build_channel_context_xml can be called; empty list
    # produces a valid <channel_context count="0"> envelope.
    message.channel.history = MagicMock(return_value=_AsyncIter([]))
    message.create_thread = AsyncMock()
    message.add_reaction = AsyncMock()
    message.attachments = []
    message.mentions = [types.SimpleNamespace(id=999)]
    return message


def _make_thread_message(
    *,
    content: str = "<@999> hello",
    guild_id: int = 123456,
    thread_id: int = 5555,
    parent_id: int = 789,
    author_id: int = 111,
) -> discord.Message:
    """Mock a message in an existing Discord thread."""
    message = MagicMock(spec=discord.Message)
    message.content = content
    message.author = MagicMock()
    message.author.bot = False
    message.author.id = author_id
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = guild_id
    # Channel is a Thread
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    thread.send = AsyncMock()
    message.channel = thread
    message.add_reaction = AsyncMock()
    message.attachments = []
    message.mentions = [types.SimpleNamespace(id=999)]
    return message


def _stub_resolved_config(
    agent_name: str | None = "test-agent",
    environment_name: str | None = "test-env",
) -> ResolvedConfig:
    return ResolvedConfig(
        agent_name=agent_name,
        agent_name_tier="tenant" if agent_name else None,
        environment_name=environment_name,
        environment_name_tier="tenant" if environment_name else None,
    )


def _make_fake_agent(name: str = "test-agent") -> BetaManagedAgentsAgent:
    """Build a validated BetaManagedAgentsAgent for MA lookup mocks."""
    return BetaManagedAgentsAgent(
        id="ag_test",
        version=1,
        name=name,
        type="agent",
        model=_AgentModelConfig(id="claude-sonnet-4-5"),
        created_at=datetime(2026, 4, 28, tzinfo=UTC),
        updated_at=datetime(2026, 4, 28, tzinfo=UTC),
        mcp_servers=[],
        metadata={},
        skills=[],
        tools=[],
    )


def _make_fake_environment(name: str = "test-env") -> BetaEnvironment:
    """Build a validated BetaEnvironment for MA lookup mocks."""
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


async def _setup_workspace_and_config(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    guild_id: str = "123456",
) -> None:
    """Seed trial credit so the balance gate (D-14) allows turns in these tests.

    The tenant must already exist with platform="discord", external_id=guild_id
    so that derive_tenant_uuid("discord", guild_id) matches. This function only
    adds the balance entry; caller is responsible for tenant creation.
    """
    _ = guild_id  # kept for call-site compatibility; tenant already keyed on derive
    # Seed a positive balance so the balance gate allows turns.
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant_id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant_id}",
    )
    await db_session.flush()


class TestNewThreadCreation:
    """Channel mentions create threads and run turns."""

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_mention_in_channel_creates_thread_and_runs_turn(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-abc")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        message.create_thread.assert_called_once_with(  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
            name="Chat with test-agent",
            auto_archive_duration=10080,
        )
        mock_create_session.assert_called_once()
        mock_run_turn.assert_called_once()
        call_kwargs = mock_run_turn.call_args.kwargs
        assert call_kwargs["render_interval_s"] == 2.0, "render interval should be 2s for Discord"
        user_message: str = call_kwargs["user_message"]
        assert "<channel_context" in user_message, (
            "channel mention must produce a <channel_context> envelope, not raw message content"
        )
        # Trigger content appears in <user_query>, not the raw message
        assert "<user_query" in user_message, "channel context must include a <user_query> element"
        assert "hello" in user_message, "trigger content must appear somewhere in the user message"
        assert call_kwargs["session_id"] == "sess-abc", "should use ma_session.id"

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_thread_and_status_embed_posted_before_session_create(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """sessions.create can hold its response for minutes (MA-side provisioning);
        the thread and a thinking embed must already be visible before that await."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()

        order: list[str] = []
        first_send_kwargs: dict[str, object] = {}

        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999

        async def _record_thread_send(*args: object, **kwargs: object) -> MagicMock:
            if not order or order[-1] != "embed_posted":
                first_send_kwargs.update(kwargs)
            order.append("embed_posted")
            return MagicMock()

        mock_thread.send = AsyncMock(side_effect=_record_thread_send)

        async def _record_create_thread(*args: object, **kwargs: object) -> MagicMock:
            order.append("thread_created")
            return mock_thread

        message.create_thread = AsyncMock(side_effect=_record_create_thread)  # pyright: ignore[reportAttributeAccessIssue]

        async def _record_create_session(
            *args: object, **kwargs: object
        ) -> BetaManagedAgentsSession:
            order.append("session_created")
            return _make_fake_session("sess-order")

        mock_create_session.side_effect = _record_create_session

        await bot.on_message(message)

        assert order[:3] == ["thread_created", "embed_posted", "session_created"], (
            f"thread + status embed must precede sessions.create, got {order}"
        )
        assert "embeds" in first_send_kwargs, "instant feedback should be an embed, not text"
        embed = cast("list[discord.Embed]", first_send_kwargs["embeds"])[0]
        assert embed.title is not None and "thinking" in embed.title, (
            "initial embed should show the thinking phase"
        )

    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_missing_config_sends_error_no_thread(
        self,
        mock_resolve: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        # Seed balance so the balance gate allows this turn (the test checks config error).
        await tenant_ledger.insert_entry(
            db_session,
            tenant_id=tenant.id,
            delta_usd=Decimal("100.00"),
            reason="trial",
            idempotency_key=f"trial:{tenant.id}",
        )
        await db_session.flush()

        mock_resolve.return_value = _stub_resolved_config(agent_name=None, environment_name=None)

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()

        await bot.on_message(message)

        message.create_thread.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        message.channel.send.assert_called_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        sent_text: str = message.channel.send.call_args[0][0]  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]
        assert "agent" in sent_text, "error should mention missing agent"
        assert "environment" in sent_text, "error should mention missing environment"
        # CR-01: recovery hint points at the /agent-setup panel, not the deleted /propagate.
        assert "/agent-setup" in sent_text, "recovery hint should point at /agent-setup"
        assert "/propagate" not in sent_text, "the deleted /propagate command must not be suggested"

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_session_creation_failure_sends_error_after_thread_created(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The thread + status embed go up before sessions.create, so a session
        creation failure happens after the thread exists; the error still renders
        through the boundary handler and the turn never runs."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.side_effect = _anthropic.APIConnectionError(request=MagicMock())
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        message.create_thread.assert_called_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        assert any("embeds" in c.kwargs for c in mock_thread.send.call_args_list), (
            "status embed should have been posted before the session create failed"
        )
        message.channel.send.assert_called_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        error_text: str = message.channel.send.call_args[0][0]  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]
        assert "rid:" in error_text, "error should use render_error with ULID suffix"
        assert "Connection Error" in error_text, "should render APIConnectionError"
        mock_run_turn.assert_not_called()


class TestThreadMention:
    """Thread mentions respond in-place with XML history context."""

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_mention_in_thread_responds_in_place(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_build_xml: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Thread mentions run a turn without creating a new thread."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-thread")
        mock_build_xml.return_value = (
            "<context><thread_history></thread_history></context>\n\n<user_query>hello</user_query>",
            [],
        )
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_thread_message()

        await bot.on_message(message)

        mock_create_session.assert_called_once()
        mock_build_xml.assert_called_once()
        mock_run_turn.assert_called_once()

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_mention_in_thread_passes_xml_context_to_run_turn(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_build_xml: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Thread mention passes XML history as user_message to run_turn."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-xml")
        fake_xml = (
            "<context><thread_history><message>prior</message></thread_history></context>"
            "\n\n<user_query>trigger</user_query>"
        )
        mock_build_xml.return_value = (fake_xml, [])
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_thread_message()

        await bot.on_message(message)

        call_kwargs = mock_run_turn.call_args.kwargs
        assert call_kwargs["user_message"] == fake_xml, (
            "XML context should be passed as user_message"
        )
        assert call_kwargs["session_id"] == "sess-xml", "should use ma_session.id"

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_scope_context_uses_parent_channel_id(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_build_xml: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session()
        mock_build_xml.return_value = ("<context></context>", [])
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_thread_message(parent_id=789)

        await bot.on_message(message)

        mock_resolve.assert_called_once()
        call_kwargs = mock_resolve.call_args.kwargs
        context: ScopeContext = call_kwargs["context"]
        assert context.channel_id == "789", "should use parent_id, not thread_id"

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_config_change_affects_next_turn(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_build_xml: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Config is resolved per turn for thread mentions."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session()
        mock_build_xml.return_value = ("<context></context>", [])
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_thread_message()

        await bot.on_message(message)

        # resolve_config is called on every turn
        mock_resolve.assert_called_once()
        mock_run_turn.assert_called_once()

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_each_thread_mention_creates_fresh_session(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_build_xml: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Every mention creates a new session (session-per-turn)."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session()
        mock_build_xml.return_value = ("<context></context>", [])
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_thread_message()

        await bot.on_message(message)

        mock_create_session.assert_called_once()
        # Verify new signature: no sessionmaker, no tenant_id
        call_args = mock_create_session.call_args
        assert call_args[0][0] is runtime.anthropic, (
            "first positional arg should be anthropic client"
        )
        assert "agent" in call_args.kwargs, "should pass agent kwarg"
        assert "environment" in call_args.kwargs, "should pass environment kwarg"


class TestConcurrentTurnProtection:
    """Follow-up mentions in a thread that already has a turn in-flight get
    queued, not dropped. (Channel-level mentions run in parallel instead — see
    test_mention_queue.py.)"""

    async def test_concurrent_thread_mention_gets_hourglass_reaction_and_queues(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)

        # Mark the thread as currently processing a turn (without actually
        # running one). on_message should react ⌛ and append to _pending.
        thread_id = 5555
        bot._processing.add(thread_id)  # pyright: ignore[reportPrivateUsage]

        message = _make_thread_message(thread_id=thread_id)
        await bot.on_message(message)

        message.add_reaction.assert_called_once_with("⌛")
        assert bot._pending[thread_id] == [message], (  # pyright: ignore[reportPrivateUsage]
            "concurrent thread mention must be queued for drain after the current turn finishes"
        )


class TestAutoArchive:
    """Thread auto-archive duration."""

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_thread_created_with_7_day_archive(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-auto-archive")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        message.create_thread.assert_called_once_with(  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
            name="Chat with test-agent",
            auto_archive_duration=10080,
        )


class TestHandleMentionErrorBoundary:
    """_handle_mention error boundary uses render_error with ULID request ID."""

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_turn_error_message_contains_rid_suffix(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """When run_turn raises APIConnectionError, the error message sent to the
        channel/thread must contain 'rid:' (render_error ULID suffix), not a
        plain hardcoded string."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-err")
        mock_run_turn.side_effect = _anthropic.APIConnectionError(request=MagicMock())
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        # Error caught in _handle_mention sees message.channel (TextChannel),
        # not the thread created inside _orchestrate (D-14 accepted edge case).
        message.channel.send.assert_called_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        error_text: str = message.channel.send.call_args[0][0]  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]
        assert "rid:" in error_text, (
            "error boundary should use render_error which appends rid: ULID suffix; "
            f"got: {error_text!r}"
        )

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_turn_error_message_has_structured_format(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Error from run_turn must render with emoji prefix and bold label,
        not the legacy 'An error occurred. Please try again.' plain string."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-err2")
        mock_run_turn.side_effect = _anthropic.APIConnectionError(request=MagicMock())
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9998
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        # Error caught in _handle_mention sees message.channel (TextChannel),
        # not the thread created inside _orchestrate (D-14 accepted edge case).
        message.channel.send.assert_called_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        error_text: str = message.channel.send.call_args[0][0]  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]
        assert "**" in error_text, (
            f"structured error should contain bold label via **; got: {error_text!r}"
        )
        assert "An error occurred. Please try again." not in error_text, (
            f"should not use legacy hardcoded error string; got: {error_text!r}"
        )


class TestSetupHook:
    """setup_hook loads the remaining command Cogs."""

    async def test_setup_hook_loads_remaining_cogs(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)

        # Stub remaining Cogs via sys.modules to keep the test merge-order-independent.
        mock_help_cog = MagicMock()
        mock_agent_setup_cog = MagicMock()
        mock_routines_cog = MagicMock()
        mock_billing_cog = MagicMock()
        mock_privacy_cog = MagicMock()

        help_mod = types.ModuleType("daimon.adapters.discord.commands.help")
        help_mod.HelpCog = mock_help_cog  # type: ignore[attr-defined]
        agent_setup_mod = types.ModuleType("daimon.adapters.discord.commands.agent_setup")
        agent_setup_mod.AgentSetupCog = mock_agent_setup_cog  # type: ignore[attr-defined]
        routines_mod = types.ModuleType("daimon.adapters.discord.commands.routines")
        routines_mod.RoutinesCog = mock_routines_cog  # type: ignore[attr-defined]
        billing_mod = types.ModuleType("daimon.adapters.discord.commands.billing")
        billing_mod.BillingCog = mock_billing_cog  # type: ignore[attr-defined]
        privacy_mod = types.ModuleType("daimon.adapters.discord.commands.privacy")
        privacy_mod.PrivacyCog = mock_privacy_cog  # type: ignore[attr-defined]

        add_cog_calls: list[object] = []

        async def tracking_add_cog(cog: object, **kwargs: object) -> None:
            add_cog_calls.append(cog)

        bot.add_cog = tracking_add_cog  # type: ignore[assignment]

        with patch.dict(
            "sys.modules",
            {
                "daimon.adapters.discord.commands.help": help_mod,
                "daimon.adapters.discord.commands.agent_setup": agent_setup_mod,
                "daimon.adapters.discord.commands.routines": routines_mod,
                "daimon.adapters.discord.commands.billing": billing_mod,
                "daimon.adapters.discord.commands.privacy": privacy_mod,
            },
        ):
            await bot.setup_hook()

        assert len(add_cog_calls) == 5, "setup_hook should add exactly 5 Cogs"
        mock_help_cog.assert_called_once_with(bot)
        mock_agent_setup_cog.assert_called_once_with(bot)
        mock_routines_cog.assert_called_once_with(bot)
        mock_billing_cog.assert_called_once_with(bot)
        mock_privacy_cog.assert_called_once_with(bot)


@pytest.mark.skip(
    reason="per-turn session deletion disabled by hotfix 6703918c — re-enable when restored"
)
class TestSessionCleanup:
    """MA session is deleted on every exit path after create_session succeeds."""

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_session_deleted_on_successful_turn(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-cleanup")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9001
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        (  # pyright: ignore[reportUnusedExpression]
            runtime.anthropic.beta.sessions.delete.assert_called_once_with("sess-cleanup"),  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
            "session should be deleted after successful turn",
        )

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_session_deleted_when_run_turn_raises(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-err-cleanup")
        mock_run_turn.side_effect = _anthropic.APIConnectionError(request=MagicMock())
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9002
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        (  # pyright: ignore[reportUnusedExpression]
            runtime.anthropic.beta.sessions.delete.assert_called_once_with("sess-err-cleanup"),  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
            "session should be deleted even when run_turn raises",
        )

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_session_deleted_when_create_thread_raises(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-thread-fail")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()
        resp = MagicMock()
        resp.status = 500
        message.create_thread.side_effect = discord.HTTPException(  # pyright: ignore[reportAttributeAccessIssue]
            response=resp, message="thread fail"
        )

        await bot.on_message(message)

        (  # pyright: ignore[reportUnusedExpression]
            runtime.anthropic.beta.sessions.delete.assert_called_once_with("sess-thread-fail"),  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
            "session should be deleted when thread creation fails",
        )
        mock_run_turn.assert_not_called(), "run_turn should not execute when thread creation fails"  # pyright: ignore[reportUnusedExpression]

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_cleanup_failure_does_not_propagate(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-cleanup-fail")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        runtime.anthropic.beta.sessions.delete.side_effect = Exception("delete failed")  # pyright: ignore[reportAttributeAccessIssue]
        bot = _make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9004
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        # Should complete without raising despite cleanup failure
        await bot.on_message(message)

        mock_run_turn.assert_called_once(), "turn should complete normally despite cleanup failure"  # pyright: ignore[reportUnusedExpression]

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_cleanup_failure_does_not_mask_original_error(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-double-fail")
        mock_run_turn.side_effect = _anthropic.APIConnectionError(request=MagicMock())
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        runtime.anthropic.beta.sessions.delete.side_effect = Exception("delete also failed")  # pyright: ignore[reportAttributeAccessIssue]
        bot = _make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9005
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        # The original run_turn error (APIConnectionError) should have been caught by
        # _handle_mention and rendered to the channel as "Connection Error"
        (  # pyright: ignore[reportUnusedExpression]
            message.channel.send.assert_called_once(),  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
            "original run_turn error should propagate, not cleanup error",
        )
        error_text: str = message.channel.send.call_args[0][0]  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]
        assert "Connection Error" in error_text, (
            "original run_turn error should propagate, not cleanup error"
        )

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_cleanup_failure_emits_structured_log(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-log-check")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        runtime.anthropic.beta.sessions.delete.side_effect = Exception("delete failed")  # pyright: ignore[reportAttributeAccessIssue]
        bot = _make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9006
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        with structlog.testing.capture_logs() as logs:
            await bot.on_message(message)

        cleanup_logs = [e for e in logs if e["event"] == "session.cleanup_failed"]
        assert len(cleanup_logs) == 1, "should emit exactly one cleanup_failed log"
        entry = cleanup_logs[0]
        assert entry["session_id"] == "sess-log-check", "log should contain session_id"
        assert "delete failed" in entry["error"], "log should contain error message"


class TestBillingAdmissionGate:
    """Phase 20-08: is_over_cap admission gate + usage_record wiring."""

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.is_over_cap", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_over_cap_skips_turn(
        self,
        mock_resolve: AsyncMock,
        mock_is_over_cap: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """When is_over_cap returns True, no MA session is created and no turn runs."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_is_over_cap.return_value = True
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()

        await bot.on_message(message)

        mock_is_over_cap.assert_awaited_once()
        cap_kwargs = mock_is_over_cap.call_args.kwargs
        assert cap_kwargs["tenant_id"] == tenant.id, "cap check must be keyed on tenant_id"
        assert cap_kwargs["user_id"] == "111", "gate should pass user_id from message author"
        mock_create_session.assert_not_called()
        mock_run_turn.assert_not_called()
        message.create_thread.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        message.channel.send.assert_called_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        sent_text: str = message.channel.send.call_args[0][0]  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]
        assert (
            "cap" in sent_text.lower()  # pyright: ignore[reportUnknownMemberType]
        ), f"over-cap message should mention 'cap'; got: {sent_text!r}"

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.is_over_cap", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_under_cap_proceeds_normally(
        self,
        mock_resolve: AsyncMock,
        mock_is_over_cap: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Under cap: gate returns False; create_session + run_turn fire normally."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_is_over_cap.return_value = False
        mock_create_session.return_value = _make_fake_session("sess-undercap")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        mock_is_over_cap.assert_awaited_once()
        mock_create_session.assert_called_once()
        mock_run_turn.assert_called_once()

    @patch("daimon.adapters.discord.bot.is_over_cap", new_callable=AsyncMock)
    async def test_dm_no_gate_short_circuit(
        self,
        mock_is_over_cap: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """DM (message.guild is None) short-circuits in should_process_message
        BEFORE the gate is consulted. Regression guard: gate must not be called."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()
        message.guild = None  # DM

        await bot.on_message(message)

        mock_is_over_cap.assert_not_called()

    # TODO(phase-38-followup): migrate to MARouter transport-level fake
    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.is_over_cap", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_usage_record_wired_with_session_model_id(
        self,
        mock_resolve: AsyncMock,
        mock_is_over_cap: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """usage_record passed to run_turn is functools.partial bound to
        ma_session.id and ma_session.agent.model.id."""
        import functools as _functools

        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_is_over_cap.return_value = False
        mock_create_session.return_value = _make_fake_session("sess-usage")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        mock_run_turn.assert_called_once()
        call_kwargs = mock_run_turn.call_args.kwargs
        usage_record = call_kwargs["usage_record"]
        assert isinstance(usage_record, _functools.partial), (
            "usage_record should be a functools.partial"
        )
        bound = usage_record.keywords
        assert bound["tenant_id"] == tenant.id, "usage recording must bind tenant_id"
        assert bound["platform_user_id"] == "111", "platform_user_id should be bound"
        assert bound["managed_session_id"] == "sess-usage", (
            "managed_session_id should be ma_session.id"
        )
        assert bound["model_id"] == "claude-sonnet-4-5", (
            "model_id should be ma_session.agent.model.id"
        )


class TestResolverSelfHeal:
    """Real ma_resolver runs end-to-end; archived cached id self-heals to live tag-matched id."""

    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_discord_resolves_via_ma_resolver_end_to_end(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """When MA archives the resource that would normally be returned,
        resolve_agent / resolve_environment fall through to tag lookup and
        return the live id. Discord replies (no 'no longer exists' error)."""
        import httpx
        from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
        from daimon.core.ma_resolver import new_resolver_cache
        from daimon.testing.ma import (
            EMPTY_CLOUD_CONFIG,
            MARouter,
            build_fake_anthropic,
            list_response,
        )

        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        await db_session.commit()

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-heal")

        live_agent = BetaManagedAgentsAgent(
            id="ag_live",
            version=1,
            name="test-agent",
            type="agent",
            model=_AgentModelConfig(id="claude-sonnet-4-5"),
            created_at=datetime(2026, 5, 19, tzinfo=UTC),
            updated_at=datetime(2026, 5, 19, tzinfo=UTC),
            mcp_servers=[],
            metadata={
                MA_METADATA_KEY_TENANT: str(tenant.id),
                MA_METADATA_KEY_NAME: "test-agent",
            },
            skills=[],
            tools=[],
        ).model_dump(mode="json")
        live_env = BetaEnvironment(
            id="env_live",
            name="test-env",
            type="environment",
            config=EMPTY_CLOUD_CONFIG,
            created_at="2026-05-19T00:00:00Z",
            updated_at="2026-05-19T00:00:00Z",
            description="",
            metadata={
                MA_METADATA_KEY_TENANT: str(tenant.id),
                MA_METADATA_KEY_NAME: "test-env",
            },
        ).model_dump(mode="json")

        router = MARouter()
        router.add(
            "GET", r"/v1/agents/ag_live", lambda req, _m: httpx.Response(200, json=live_agent)
        )
        router.add(
            "GET",
            r"/v1/environments/env_live",
            lambda req, _m: httpx.Response(200, json=live_env),
        )
        router.add("GET", r"/v1/agents", lambda req, _m: list_response([live_agent]))
        router.add("GET", r"/v1/environments", lambda req, _m: list_response([live_env]))

        anthropic = build_fake_anthropic(router.dispatch)
        # Override runtime with the real fake_anthropic for this test.
        settings = MagicMock()
        settings.mcp = McpSettings()
        settings.defaults_root = tmp_path
        settings.billing.markup = Decimal("1.0")
        settings.billing.signup_credit = Decimal("0")
        discord_settings = MagicMock()
        discord_settings.max_concurrent_turns_per_tenant = 100  # effectively uncapped in tests
        settings.discord = discord_settings
        runtime = DiscordRuntime(
            settings=settings,
            anthropic=anthropic,
            sessionmaker=db_session_factory,
            notebook_rate_limiter=RateLimiter(max_requests=999),
            billing_config=None,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
        )

        bot = _make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 7777
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        # Self-heal: bot found the live agent/env via tag lookup; create_session
        # was called (resolver returned a live id, retrieve succeeded), and the
        # bot ran a turn rather than sending the "no longer exists" error.
        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        mock_create_session.assert_called_once()
        mock_run_turn.assert_called_once()
        # The retrieve-by-id path returned the live agent (id starts with ag_live).
        agent_kwarg = mock_create_session.call_args.kwargs["agent"]
        env_kwarg = mock_create_session.call_args.kwargs["environment"]
        assert agent_kwarg.id == "ag_live", "resolver returned live id, re-retrieve loaded it"
        assert env_kwarg.id == "env_live", "resolver returned live env id, re-retrieve loaded it"

    @patch("daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_resolve_ids_tag_miss_wires_guild_tenant_id_and_public_url_into_self_heal(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_reconcile: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """G2 regression: when _resolve_ids fires a tag miss, the self-heal closure must
        pass (a) the MESSAGE's guild-derived tenant_id and (b) settings.mcp.public_url.

        Breaking the lambda (wrong tenant or dropped public_url) turns this test red.
        Fixes #130 secondary: alternating self-heals cannot flip the spec hash if every
        reconcile sees the same public_url that the guild-join seed used.
        """
        import re

        import httpx
        from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.ma_resolver import new_resolver_cache
        from daimon.testing.ma import (
            EMPTY_CLOUD_CONFIG,
            MARouter,
            build_fake_anthropic,
            list_response,
        )

        workspace_id = "123456"
        tenant = await make_tenant(db_session, platform="discord", workspace_id=workspace_id)
        await _setup_workspace_and_config(db_session, tenant.id)
        await db_session.commit()

        expected_tenant_id = derive_tenant_uuid(platform="discord", workspace_id=workspace_id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-miss-heal")

        live_agent = BetaManagedAgentsAgent(
            id="ag_miss_live",
            version=1,
            name="test-agent",
            type="agent",
            model=_AgentModelConfig(id="claude-sonnet-4-5"),
            created_at=datetime(2026, 5, 19, tzinfo=UTC),
            updated_at=datetime(2026, 5, 19, tzinfo=UTC),
            mcp_servers=[],
            metadata={
                MA_METADATA_KEY_TENANT: str(tenant.id),
                MA_METADATA_KEY_NAME: "test-agent",
            },
            skills=[],
            tools=[],
        ).model_dump(mode="json")
        live_env = BetaEnvironment(
            id="env_miss_live",
            name="test-env",
            type="environment",
            config=EMPTY_CLOUD_CONFIG,
            created_at="2026-05-19T00:00:00Z",
            updated_at="2026-05-19T00:00:00Z",
            description="",
            metadata={
                MA_METADATA_KEY_TENANT: str(tenant.id),
                MA_METADATA_KEY_NAME: "test-env",
            },
        ).model_dump(mode="json")

        # Stateful list handlers: return empty (tag miss) until reconcile fires, then live.
        agent_applied: list[bool] = [False]
        env_applied: list[bool] = [False]

        def agent_list_handler(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
            if agent_applied[0]:
                return list_response([live_agent])
            return list_response([])

        def env_list_handler(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
            if env_applied[0]:
                return list_response([live_env])
            return list_response([])

        # reconcile_tenant_defaults side_effect: flip both applied flags so the
        # retry-list handlers return live payloads on the next lookup.
        async def reconcile_side_effect(*_args: object, **kwargs: object) -> None:
            agent_applied[0] = True
            env_applied[0] = True

        mock_reconcile.side_effect = reconcile_side_effect

        router = MARouter()
        router.add(
            "GET",
            r"/v1/agents/ag_miss_live",
            lambda req, _m: httpx.Response(200, json=live_agent),
        )
        router.add(
            "GET",
            r"/v1/environments/env_miss_live",
            lambda req, _m: httpx.Response(200, json=live_env),
        )
        router.add("GET", r"/v1/agents", agent_list_handler)
        router.add("GET", r"/v1/environments", env_list_handler)

        anthropic = build_fake_anthropic(router.dispatch)
        settings = MagicMock()
        # Plain string on the MagicMock: bot.py applies str(), identity on str — assertion stays literal.
        settings.mcp.public_url = "https://example.test/mcp"
        settings.defaults_root = tmp_path
        settings.billing.markup = Decimal("1.0")
        settings.billing.signup_credit = Decimal("0")
        discord_settings = MagicMock()
        discord_settings.max_concurrent_turns_per_tenant = 100
        settings.discord = discord_settings
        runtime = DiscordRuntime(
            settings=settings,
            anthropic=anthropic,
            sessionmaker=db_session_factory,
            notebook_rate_limiter=RateLimiter(max_requests=999),
            billing_config=None,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
        )

        bot = _make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 8888
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        assert mock_reconcile.await_count >= 1, "tag miss must fire the self-heal reconcile"
        for call in mock_reconcile.await_args_list:
            call_kwargs = call.kwargs
            assert call_kwargs["tenant_id"] == expected_tenant_id, (
                "self-heal closure must reconcile the MESSAGE's guild tenant, not any other"
            )
            assert call_kwargs["public_url"] == "https://example.test/mcp", (
                "self-heal closure must thread settings.mcp.public_url — spec-hash flip-flop guard, #130"
            )


class TestAttachmentOrchestration:
    """Non-image attachments surface their signed CDN URL in the user message."""

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_data_attachment_cdn_url_prefix_prepended_to_user_message(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-attach")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()
        # A non-image attachment routes to the data path → signed CDN URL prefix.
        fake_attachment = MagicMock(spec=discord.Attachment)
        fake_attachment.filename = "x.csv"
        fake_attachment.size = 5
        fake_attachment.url = "https://cdn.discord/x.csv?ex=1&is=2&hm=3"
        fake_attachment.content_type = "text/csv"
        fake_attachment.width = None
        fake_attachment.height = None
        message.attachments = [fake_attachment]
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 5050
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread

        await bot.on_message(message)

        mock_run_turn.assert_called_once()
        user_msg: str = mock_run_turn.call_args.kwargs["user_message"]
        assert user_msg.startswith("*system: user attached `x.csv`"), (
            "data attachment CDN-URL prefix must be prepended to the user message"
        )
        assert "https://cdn.discord/x.csv" in user_msg, "the signed CDN URL must be surfaced"
        # Channel mentions now produce a <channel_context> envelope; trigger content appears
        # in <user_query> at the end, not as raw message.content
        assert "<channel_context" in user_msg, (
            "channel mention wraps context in <channel_context> envelope"
        )
        assert "hello" in user_msg, "trigger content must be preserved in user_query"

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_trigger_image_cdn_url_prefix_prepended_to_user_message(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A trigger image rides along as a vision block AND its signed CDN URL
        lands in the user message — the URL is the agent's only byte-level
        handle for forwarding the image to external APIs."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session("sess-image-url")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        @dataclass
        class FakeImageAttachment:
            filename: str
            content_type: str
            size: int
            url: str
            id: int = 42
            width: int | None = 800
            height: int | None = 600

            async def read(self) -> bytes:
                return b"\x89PNG\r\n\x1a\n"

        signed_url = "https://cdn.discordapp.com/attachments/789/42/chart.png?ex=a&is=b&hm=c"
        attachment = FakeImageAttachment(
            filename="chart.png", content_type="image/png", size=4, url=signed_url
        )

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_channel_message()
        message.attachments = [cast(discord.Attachment, attachment)]
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 5053
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread

        await bot.on_message(message)

        mock_run_turn.assert_called_once()
        user_msg: str = mock_run_turn.call_args.kwargs["user_message"]
        assert user_msg.startswith("*system: user attached image `chart.png`"), (
            "image URL prefix must be prepended to user message"
        )
        assert signed_url in user_msg, "full signed CDN URL must reach the agent"
        # Channel mentions now produce a <channel_context> envelope; trigger content appears
        # in <user_query> at the end, not as raw message.content
        assert "<channel_context" in user_msg, (
            "channel mention wraps context in <channel_context> envelope"
        )
        assert "hello" in user_msg, "trigger content must be preserved in user_query"
        image_blocks = mock_run_turn.call_args.kwargs["image_blocks"]
        assert image_blocks is not None and len(image_blocks) == 1, (
            "image must still be forwarded as a vision block alongside the URL line"
        )


class TestSessionReuse:
    """SC-1 and SC-4: session-per-thread reuse + 404 recreate."""

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_delta_xml", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_second_thread_mention_reuses_session_no_second_create(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_build_delta: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """SC-1: second mention on an existing thread reuses the session — no create_session call."""
        from daimon.core.stores.identity import get_or_create_platform_principal
        from daimon.core.stores.thread_sessions import create_thread_session

        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        # Pre-create the principal so we know account_id before seeding the thread row.
        # The bot uses external_id=str(message.author.id); _make_thread_message default is 111.
        principal = await get_or_create_platform_principal(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            external_id="111",
        )
        await db_session.commit()

        # Seed a live thread_sessions row so the second mention finds it.
        existing_session_id = "sesn_existing_001"
        watermark_msg_id = "111222333"
        async with db_session_factory() as seed_session:
            await create_thread_session(
                seed_session,
                tenant_id=tenant.id,
                platform="discord",
                thread_id="5555",  # matches _make_thread_message default thread_id
                account_id=principal.account_id,
                ma_session_id=existing_session_id,
                watermark_message_id=watermark_msg_id,
            )
            await seed_session.commit()

        mock_resolve.return_value = _stub_resolved_config()
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        mock_build_delta.return_value = (
            "<context><thread_delta></thread_delta></context>\n\n<user_query>hello</user_query>",
            [],
        )

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_thread_message(thread_id=5555)

        await bot.on_message(message)

        mock_create_session.assert_not_called()
        mock_run_turn.assert_called_once()
        call_kwargs = mock_run_turn.call_args.kwargs
        assert call_kwargs["session_id"] == existing_session_id, (
            "run_turn must receive the reused ma_session_id"
        )
        assert "<thread_delta>" in call_kwargs["user_message"], (
            "continuation turn must use delta context, not full history"
        )

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_first_thread_mention_creates_and_persists_mapping_and_watermark(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_build_xml: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """SC-1: first thread mention creates a session, persists the mapping row,
        and writes the watermark after a successful turn."""
        from daimon.core.stores.identity import get_or_create_platform_principal
        from daimon.core.stores.thread_sessions import get_live_thread_session

        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        # Pre-create the principal so account_id is deterministic for the verification query.
        pre_principal = await get_or_create_platform_principal(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            external_id="111",  # matches _make_thread_message default author_id
        )
        await db_session.commit()

        new_session_id = "sesn_new_first_001"
        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session(new_session_id)
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        mock_build_xml.return_value = (
            "<context><thread_history></thread_history></context>\n\n<user_query>hi</user_query>",
            [],
        )

        # Wire the lifecycle's final_message_id by mocking send to return a message with id.
        bot_reply_msg = MagicMock(spec=discord.Message)
        bot_reply_msg.id = 777888999

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_thread_message(thread_id=5556)

        # Inject the fake reply message so lifecycle.final_message_id is not None.
        # We patch the thread.send to return the mock message so the embed send captures it.
        thread_mock = message.channel
        thread_mock.send = AsyncMock(return_value=bot_reply_msg)

        await bot.on_message(message)

        mock_create_session.assert_called_once()
        mock_run_turn.assert_called_once()
        call_kwargs = mock_run_turn.call_args.kwargs
        assert call_kwargs["session_id"] == new_session_id, (
            "run_turn must receive the newly created ma_session_id"
        )

        # Verify a thread_sessions row was persisted (keyed by the caller's account_id).
        async with db_session_factory() as verify_session:
            row = await get_live_thread_session(
                verify_session,
                tenant_id=tenant.id,
                platform="discord",
                thread_id="5556",
                account_id=pre_principal.account_id,
            )
        assert row is not None, "thread_sessions row must be created after first mention"
        assert row.ma_session_id == new_session_id, (
            "persisted row must store the created ma_session_id"
        )

    @patch("daimon.adapters.discord.bot.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.create_session", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.resolve_config", new_callable=AsyncMock)
    async def test_dead_session_404_recreates_and_marks_old_row_dead(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_build_xml: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """SC-4: when the first run_turn returns a 404 dead-session error, the bot
        marks the old row dead, creates a new session, inserts a new live row, and
        re-runs with full history. The second run succeeds."""
        import httpx
        from daimon.core.errors import TurnError
        from daimon.core.stores.identity import get_or_create_platform_principal
        from daimon.core.stores.thread_sessions import (
            create_thread_session,
            get_live_thread_session,
        )
        from daimon.core.turn.state import TurnState

        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        # Pre-create the principal so account_id is known for both seed and verify.
        principal = await get_or_create_platform_principal(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            external_id="111",  # matches _make_thread_message default author_id
        )
        await db_session.commit()

        # Seed a live row for the thread.
        old_session_id = "sesn_old_dead_001"
        async with db_session_factory() as seed_session:
            await create_thread_session(
                seed_session,
                tenant_id=tenant.id,
                platform="discord",
                thread_id="5557",
                account_id=principal.account_id,
                ma_session_id=old_session_id,
                watermark_message_id="100",
            )
            await seed_session.commit()

        new_session_id = "sesn_new_recreated_001"
        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = _make_fake_session(new_session_id)
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        mock_build_xml.return_value = (
            "<context><thread_history></thread_history></context>\n\n<user_query>hi</user_query>",
            [],
        )

        # Build a real APIStatusError with status_code == 404.
        fake_request = MagicMock(spec=httpx.Request)
        fake_response = httpx.Response(404, json={"type": "not_found_error", "message": "gone"})
        fake_response.request = fake_request
        dead_cause = _anthropic.APIStatusError(
            "Session not found", response=fake_response, body={"type": "not_found_error"}
        )
        dead_state = TurnState(
            error=TurnError(kind="upstream", message="Session not found", cause=dead_cause)
        )
        success_state = TurnState()

        # First call returns dead state; second call returns success.
        mock_run_turn.side_effect = [dead_state, success_state]

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = _make_bot(runtime)
        message = _make_thread_message(thread_id=5557)

        await bot.on_message(message)

        assert mock_run_turn.call_count == 2, (
            "run_turn must be called twice: first attempt (dead) + recreate retry (success)"
        )
        # Second run_turn must use the new session id.
        second_call_kwargs = mock_run_turn.call_args_list[1].kwargs
        assert second_call_kwargs["session_id"] == new_session_id, (
            "recreate retry must use the new session id"
        )
        # The second user_message must be full history (not delta).
        assert "<thread_history>" in second_call_kwargs["user_message"], (
            "recreate retry must re-seed with full history, not delta"
        )
        # create_session must have been called once for the recreate.
        mock_create_session.assert_called_once()

        # Old row must be dead; a new live row must exist (keyed to the caller's account).
        async with db_session_factory() as verify_session:
            live_row = await get_live_thread_session(
                verify_session,
                tenant_id=tenant.id,
                platform="discord",
                thread_id="5557",
                account_id=principal.account_id,
            )
        assert live_row is not None, "a new live row must exist after recreate"
        assert live_row.ma_session_id == new_session_id, (
            "new live row must store the recreated session id"
        )
