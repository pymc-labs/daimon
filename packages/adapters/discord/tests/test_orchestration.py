"""Tests for on_message turn orchestration in DaimonBot."""

from __future__ import annotations

import asyncio
import contextlib
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
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.views import CancelView
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import McpSettings, ThreadNamingSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import ResolverCache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault, ResolvedConfig, ScopeContext
from daimon.core.session_snapshot import (
    SessionSnapshot,
    fingerprint_identity,
    fingerprint_mutable,
    snapshot_from_created_session,
)
from daimon.core.stores import tenant_ledger
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.tenants import set_provision_status
from daimon.core.stores.turn_card_intents import (
    TurnCardIntentRow,
    list_recoverable_turn_card_intents,
)
from daimon.core.turn.deps import TurnDeps, build_turn_deps
from daimon.core.turn_queue import TurnTicket
from daimon.testing import (
    DEFAULT_MODEL_ID,
    ma_agent,
    ma_environment,
    ma_session,
)
from daimon.testing.factories import make_channel_budget
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import make_tenant
from .harness import make_bot


def _snapshot_of(session: BetaManagedAgentsSession) -> SessionSnapshot:
    """The configuration a mapping row records for the session it maps to.

    A row seeded without one reads as pre-continuity and makes the bind read
    the session from MA, which these tests' bare `AsyncMock` client cannot
    answer.
    """
    return snapshot_from_created_session(
        session,
        env_sha256=None,
        env_file_id=None,
        repo_token_issued_at=None,
        vault_id=None,
        sent_skills=session.agent.skills,
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
    _ = tenant_id  # runtime no longer carries tenant_id; bot.py threads it
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.billing.markup = Decimal("1.0")
    settings.billing.signup_credit = Decimal("0")
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 100  # effectively uncapped in tests
    discord_settings.thread_open_notice_after_s = 3.0
    settings.discord = discord_settings
    settings.thread_naming = ThreadNamingSettings(enabled=False)
    anthropic = AsyncMock()
    anthropic.beta.agents.retrieve = AsyncMock(return_value=ma_agent())
    anthropic.beta.environments.retrieve = AsyncMock(return_value=ma_environment())
    # Dead-session recovery's transcript rescue (`_replay_previous_session`)
    # walks this as an async iterator, not an awaitable -- an unconfigured
    # AsyncMock attribute returns a coroutine instead and blows up with
    # "'async for' requires an object with __aiter__ method". Empty history
    # degrades it to the history-only rung, same as before that rescue path
    # existed.
    anthropic.beta.sessions.events.list = MagicMock(return_value=_AsyncIter([]))
    from daimon.core.ma_resolver import new_resolver_cache

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
        turn_deps=_make_turn_deps(
            settings,
            anthropic,
            sessionmaker,
            resolver_cache=resolver_cache,
            deployment_default=deployment_default,
        ),
    )


def _make_turn_deps(
    settings: MagicMock,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    resolver_cache: ResolverCache,
    deployment_default: DeploymentDefault,
) -> TurnDeps:
    """Build the TurnDeps a DiscordRuntime carries via the production
    `build_turn_deps` helper, so tests see the same derivation build_runtime
    does. `settings.crypto.keys` on an unconfigured MagicMock iterates empty
    (MagicMock auto-specs `__iter__`), so fernet is None here as in the
    pre-cutover tests."""
    return build_turn_deps(
        settings,
        anthropic,
        sessionmaker,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        billing_config=None,
    )


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


async def _setup_workspace_and_config(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    guild_id: str = "123456",
) -> None:
    """Seed trial credit so the balance gate allows turns in these tests.

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

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_thread_create_failure_releases_admission_counters(
        self,
        mock_resolve: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        mock_resolve.return_value = _stub_resolved_config()
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        runtime = _make_runtime(tenant.id, db_session_factory)
        assert runtime.settings.discord is not None
        runtime.settings.discord.thread_open_notice_after_s = 0
        bot = make_bot(runtime)
        message = _make_channel_message()
        message.reply = AsyncMock()  # pyright: ignore[reportAttributeAccessIssue]
        message.add_reaction = AsyncMock()  # pyright: ignore[reportAttributeAccessIssue]
        message.remove_reaction = AsyncMock()  # pyright: ignore[reportAttributeAccessIssue]
        message.create_thread = AsyncMock(side_effect=RuntimeError("Discord unavailable"))  # pyright: ignore[reportAttributeAccessIssue]
        # Typing is best effort: a dropped connection must not stop the open.
        message.channel.typing = MagicMock(side_effect=ConnectionResetError("reset"))

        await bot.on_message(message)

        # The failure is answered with exactly one plain reply, never a
        # channel-level "opening your chat" notice or a second error.
        message.create_thread.assert_awaited_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        message.reply.assert_awaited_once_with(  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
            "Couldn't open a thread. @mention Daimon again.", mention_author=False
        )
        message.channel.send.assert_not_awaited()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        assert bot.turn_queue.in_flight() == 0
        assert bot.turn_queue.depth() == 0
        assert bot._processing == set()  # pyright: ignore[reportPrivateUsage]

    @patch(
        "daimon.adapters.discord.bot.TurnPostRecorder.opened_thread",
        new_callable=AsyncMock,
        side_effect=RuntimeError("recording failed"),
    )
    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_a_failure_after_the_thread_exists_is_not_called_a_failed_open(
        self,
        mock_resolve: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        _mock_opened: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        mock_resolve.return_value = _stub_resolved_config()
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
        message = _make_channel_message()
        message.reply = AsyncMock()  # pyright: ignore[reportAttributeAccessIssue]
        message.add_reaction = AsyncMock()  # pyright: ignore[reportAttributeAccessIssue]
        message.remove_reaction = AsyncMock()  # pyright: ignore[reportAttributeAccessIssue]
        opened = MagicMock(spec=discord.Thread)
        opened.id = 4242
        message.create_thread = AsyncMock(return_value=opened)  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        message.create_thread.assert_awaited_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        message.reply.assert_not_awaited()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        assert bot._processing == set()  # pyright: ignore[reportPrivateUsage]

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_tool_turn_starts_output_sweep_in_its_thread(
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
        mock_create_session.return_value = ma_session(id="sess-output")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        bot = make_bot(_make_runtime(tenant.id, db_session_factory))
        message = _make_channel_message()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 9999
        thread.send = AsyncMock(
            return_value=types.SimpleNamespace(
                id=1000, edit=AsyncMock(), webhook_id=None, application_id=None
            )
        )
        message.create_thread.return_value = thread  # pyright: ignore[reportAttributeAccessIssue]

        from daimon.core.turn.lifecycle import acknowledge
        from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState

        async def finish_turn(*, lifecycle, **kwargs):
            await acknowledge(lifecycle, "accepted")
            state = TurnState(
                content=[
                    ToolUseBlock(
                        kind="tool_use",
                        id="tu_output",
                        type="agent.tool_use",
                        name="bash",
                        input={},
                    ),
                    TextBlock(kind="text", text="Done"),
                ]
            )
            await lifecycle.on_terminal_success(state)
            await acknowledge(lifecycle, "done")
            return state

        mock_run_turn.side_effect = finish_turn
        with patch(
            "daimon.adapters.discord.bot.deliver_session_outputs", new_callable=AsyncMock
        ) as deliver:
            await bot.on_message(message)
            await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]

        deliver.assert_awaited_once()
        assert deliver.await_args.args[1] is thread
        assert deliver.await_args.kwargs["session_id"] == "sess-output"
        answer = deliver.await_args.kwargs["answer"]
        assert answer is not None and answer.message_id == 1000, (
            "the files go onto the answer that carries the summary"
        )
        assert "posted" not in deliver.await_args.kwargs

    # TODO: migrate to MARouter transport-level fake
    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    @pytest.mark.parametrize("completion_enabled", [False, True])
    async def test_mention_in_channel_creates_thread_and_runs_turn(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        completion_enabled: bool,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = ma_session(id="sess-abc")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        runtime.settings.completion_pings = {tenant.id: True} if completion_enabled else {}
        bot = make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.send = AsyncMock(
            side_effect=[
                types.SimpleNamespace(id=1000, edit=AsyncMock()),
                types.SimpleNamespace(id=1001, edit=AsyncMock()),
            ]
        )
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        from daimon.core.stores.turn_card_intents import list_recoverable_turn_card_intents
        from daimon.core.turn.lifecycle import acknowledge
        from daimon.core.turn.state import TextBlock, TurnState

        async def finish_turn(*, lifecycle, **kwargs):
            await acknowledge(lifecycle, "accepted")
            state = TurnState(content=[TextBlock(kind="text", text="Done")])
            await lifecycle.on_terminal_success(state)
            await acknowledge(lifecycle, "done")
            return state

        mock_run_turn.side_effect = finish_turn
        await bot.on_message(message)
        assert mock_thread.send.await_count == (2 if completion_enabled else 1)
        assert [call.args[0] for call in message.add_reaction.await_args_list] == (
            ["👀", "✅"] if completion_enabled else ["👀"]
        )
        message.remove_reaction.assert_awaited_once_with("👀", bot.user)
        async with db_session_factory() as session:
            assert not await list_recoverable_turn_card_intents(session, platform="discord")

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
        assert '"handle": "@' in user_message, (
            "the turn controls must carry the handle people mention beside the agent name, so a "
            "renamed bot account is never read as a second agent "
            "(the exact name comes from settings; see test_branding.TestResponderHandle)"
        )
        assert call_kwargs["session_id"] == "sess-abc", "should use ma_session.id"

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_a_mention_records_its_thread_card_and_overflow_for_tidying(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Everything the turn posts names the turn's agent and the person who asked."""
        from daimon.core.ma_identity import derive_agent_uuid
        from daimon.core.stores.agent_posts import get_post, list_posts_in
        from daimon.core.stores.turn_card_intents import turn_card_intent_is_active

        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = ma_session(id="sess-tidy")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
        message = _make_channel_message(channel_id=789, author_id=111)
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.parent_id = 789
        mock_thread.send = AsyncMock(
            side_effect=[
                types.SimpleNamespace(id=1000, edit=AsyncMock()),
                types.SimpleNamespace(id=1001, edit=AsyncMock()),
            ]
        )
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        from daimon.core.turn.state import TextBlock, TurnState

        async def finish_turn(*, lifecycle, **kwargs):
            # Long enough to overflow the card into a second message.
            state = TurnState(content=[TextBlock(kind="text", text="word " * 500)])
            await lifecycle.on_terminal_success(state)
            return state

        mock_run_turn.side_effect = finish_turn
        await bot.on_message(message)

        async with db_session_factory() as session:
            thread_row = await get_post(
                session,
                tenant_id=tenant.id,
                platform="discord",
                channel_id="789",
                message_id="9999",
            )
            turn_rows = await list_posts_in(
                session,
                tenant_id=tenant.id,
                platform="discord",
                channel_id="9999",
                message_ids=["1000", "1001"],
            )
            assert thread_row is not None, "the auto-opened thread is recorded under its parent"
            by_id = {r.message_id: r for r in turn_rows}
            assert set(by_id) == {"1000", "1001"}, "the status card and the overflow are recorded"
            rows = [thread_row, *turn_rows]
            agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_test")
            assert {r.agent_id for r in rows} == {agent_uuid}, "keyed by the turn's agent"
            assert {r.requester_platform_user_id for r in rows} == {"111"}, "and who asked"
            assert (thread_row.source, thread_row.kind) == ("auto_thread", "thread")
            for message_id in ("1000", "1001"):
                row = by_id[message_id]
                assert (row.source, row.kind, row.channel_id) == ("turn", "message", "9999")
                assert row.turn_card_intent_id is not None, "each names its turn"
                assert not await turn_card_intent_is_active(session, id=row.turn_card_intent_id), (
                    "and that turn is over once the mention is handled"
                )

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    @pytest.mark.parametrize(
        ("asked", "with_files", "newer_turn"),
        [(True, True, False), (True, False, False), (False, True, False), (True, True, True)],
    )
    async def test_an_archive_asked_during_the_turn_happens_after_its_last_post(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        asked: bool,
        with_files: bool,
        newer_turn: bool,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """archive_thread on the turn's own thread is carried out after the card and files."""
        from daimon.core.stores.turn_origins import request_thread_archive
        from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
        from sqlalchemy import text

        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = ma_session(id="sess-archive")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        bot = make_bot(_make_runtime(tenant.id, db_session_factory))
        message = _make_channel_message(channel_id=789, author_id=111)
        order: list[str] = []
        card = types.SimpleNamespace(
            id=1000, edit=AsyncMock(side_effect=lambda **_: order.append("card_edit"))
        )
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.parent_id = 789
        mock_thread.send = AsyncMock(return_value=card)
        mock_thread.edit = AsyncMock(side_effect=lambda **kw: order.append(f"thread_edit:{kw}"))
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        async def finish_turn(*, lifecycle, **kwargs):
            if asked:
                # What archive_thread writes when called from inside this thread.
                async with db_session_factory.begin() as session:
                    origin_id = (
                        await session.execute(
                            text("SELECT id FROM turn_origins WHERE thread_id = '9999'")
                        )
                    ).scalar_one()
                    assert await request_thread_archive(
                        session, origin_id=origin_id, thread_id="9999", now=datetime.now(UTC)
                    )
            tool = ToolUseBlock(
                kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}
            )
            text_block = TextBlock(kind="text", text="Archiving this thread.")
            state = TurnState(content=[tool, text_block] if with_files else [text_block])
            await lifecycle.on_terminal_success(state)
            return state

        async def deliver(*_args: object, **_kwargs: object) -> None:
            await asyncio.sleep(0)
            order.append("file_posted")
            if newer_turn:
                # The sweep takes seconds; a queued mention's turn has the thread now.
                bot._processing.add(9999)  # pyright: ignore[reportPrivateUsage]

        mock_run_turn.side_effect = finish_turn
        with patch("daimon.adapters.discord.bot.deliver_session_outputs", side_effect=deliver):
            await bot.on_message(message)
            await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]

        archived = "thread_edit:{'archived': True}"
        if newer_turn:
            assert archived not in order, "archiving under a newer turn would break its card"
        elif asked:
            assert order[-1] == archived, f"the archive comes after every post, got {order}"
            assert order.count(archived) == 1 and "card_edit" in order
            if with_files:
                assert order.index("file_posted") < order.index(archived), (
                    "a delivered file would reopen the thread, so the archive waits for it"
                )
        else:
            assert archived not in order, "no archive without a request"

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        bot = make_bot(runtime)
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
            return ma_session(id="sess-order")

        mock_create_session.side_effect = _record_create_session

        await bot.on_message(message)

        assert order[:3] == ["thread_created", "embed_posted", "session_created"], (
            f"thread + status embed must precede sessions.create, got {order}"
        )
        assert "embeds" in first_send_kwargs, "instant feedback should be an embed, not text"
        embed = cast("list[discord.Embed]", first_send_kwargs["embeds"])[0]
        assert embed.title == "Working on it…", "initial embed should show the thinking phase"

    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        bot = make_bot(runtime)
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
        assert "operator" not in sent_text and "admin of this server or channel" in sent_text, (
            "a server or channel admin picks the environment, not the operator"
        )

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.retire_terminal_turn_card", new_callable=AsyncMock)
    async def test_session_creation_failure_turns_the_thread_card_into_the_error(
        self,
        retire: AsyncMock,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The thread + status card go up before sessions.create, so a session
        creation failure happens after the thread exists. The error replaces the
        card in the thread (no Stop button left spinning, intent retired) and
        nothing is posted in the parent channel."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.side_effect = _anthropic.APIConnectionError(request=MagicMock())
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
        message = _make_channel_message()
        card = MagicMock(spec=discord.Message)
        card.id = 4242
        card.webhook_id = None
        card.edit = AsyncMock(return_value=card)
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.send = AsyncMock(return_value=card)
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        message.create_thread.assert_called_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        assert any("embeds" in c.kwargs for c in mock_thread.send.call_args_list), (
            "status embed should have been posted before the session create failed"
        )
        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        card.edit.assert_awaited_once()
        edit_kwargs = card.edit.call_args.kwargs
        assert edit_kwargs["view"] is None and edit_kwargs["embed"] is None
        error_text: str = edit_kwargs["content"]
        assert "rid:" not in error_text, "trace ids stay in logs"
        assert "couldn't reach Claude" in error_text, (
            "the connection failure should have a plain retry instruction"
        )
        assert mock_thread.send.await_count == 1, "the error edits the card, it is not a 2nd post"
        retire.assert_awaited_once()
        assert retire.call_args.kwargs["expected_message_id"] == "4242"
        mock_run_turn.assert_not_called()

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.retire_terminal_turn_card", new_callable=AsyncMock)
    async def test_failure_with_an_uneditable_card_posts_the_error_in_the_thread(
        self,
        retire: AsyncMock,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """If the card cannot be edited the error still lands in the thread, not
        the parent channel, and the card's intent stays for recovery."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.side_effect = _anthropic.APIConnectionError(request=MagicMock())
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
        message = _make_channel_message()
        card = MagicMock(spec=discord.Message)
        card.id = 4242
        card.webhook_id = None
        card.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.send = AsyncMock(return_value=card)
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        assert mock_thread.send.await_count == 2, "card, then the error below it"
        error_text: str = mock_thread.send.call_args.args[0]
        assert "couldn't reach Claude" in error_text
        retire.assert_not_awaited()  # the card is still up: recovery owns its intent
        mock_run_turn.assert_not_called()

    @patch("daimon.adapters.discord.bot.update_watermark", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_failure_after_the_answer_keeps_the_answer_and_posts_in_the_thread(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        mock_watermark: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A failure after the card became the answer never edits the answer away;
        the error goes under it in the thread, not into the parent channel."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = ma_session(id="sess-abc")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        mock_watermark.side_effect = OperationalError("update", {}, Exception("db down"))

        runtime = _make_runtime(tenant.id, db_session_factory)
        runtime.settings.completion_pings = {}
        bot = make_bot(runtime)
        message = _make_channel_message()
        card = MagicMock(spec=discord.Message)
        card.id = 4242
        card.webhook_id = None
        card.edit = AsyncMock(return_value=card)
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.send = AsyncMock(return_value=card)
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        from daimon.core.turn.state import TextBlock, TurnState

        async def finish_turn(*, lifecycle, **kwargs):
            state = TurnState(content=[TextBlock(kind="text", text="The answer")])
            await lifecycle.on_terminal_success(state)
            return state

        mock_run_turn.side_effect = finish_turn
        await bot.on_message(message)

        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        contents = [c.kwargs.get("content") for c in card.edit.call_args_list]
        assert any(c is not None and "The answer" in c for c in contents)
        db_error = "couldn't load or save this change"
        assert not any(c is not None and db_error in c for c in contents), "answer overwritten"
        error_text: str = mock_thread.send.call_args.args[0]
        assert db_error in error_text

    @patch("daimon.adapters.discord.bot.retire_terminal_turn_card", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_failure_after_the_card_was_replaced_keeps_its_intent(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        retire: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """When an earlier edit replaced the card, the error goes on the replacement
        and the original card's intent is kept for recovery, not retired."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = ma_session(id="sess-abc")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        runtime.settings.completion_pings = {}
        bot = make_bot(runtime)
        message = _make_channel_message()
        replacement = MagicMock(spec=discord.Message)
        replacement.id = 5000
        replacement.webhook_id = None
        replacement.edit = AsyncMock(return_value=replacement)
        card = MagicMock(spec=discord.Message)
        card.id = 4242
        card.webhook_id = None
        card.edit = AsyncMock(return_value=replacement)
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.send = AsyncMock(return_value=card)
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        async def replace_then_fail(*, lifecycle, **kwargs):
            inner = getattr(lifecycle, "inner", lifecycle)  # run.py's first-attempt wrapper
            await inner._edit_message(inner.message_ref, content="working")
            raise _anthropic.APIConnectionError(request=MagicMock())

        mock_run_turn.side_effect = replace_then_fail
        await bot.on_message(message)

        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        assert "rid:" in replacement.edit.call_args.kwargs["content"]
        retire.assert_not_awaited()


class TestThreadMention:
    """Thread mentions respond in-place with XML history context."""

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-thread")
        mock_build_xml.return_value = (
            "<context><thread_history></thread_history></context>\n\n<user_query>hello</user_query>",
            [],
        )
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
        message = _make_thread_message()

        await bot.on_message(message)

        mock_create_session.assert_called_once()
        mock_build_xml.assert_called_once()
        mock_run_turn.assert_called_once()

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-xml")
        fake_xml = (
            "<context><thread_history><message>prior</message></thread_history></context>"
            "\n\n<user_query>trigger</user_query>"
        )
        mock_build_xml.return_value = (fake_xml, [])
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
        message = _make_thread_message()

        await bot.on_message(message)

        call_kwargs = mock_run_turn.call_args.kwargs
        assert call_kwargs["user_message"].endswith(fake_xml), (
            "platform history should supplement the trusted per-turn control context"
        )
        assert call_kwargs["session_id"] == "sess-xml", "should use ma_session.id"

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session()
        mock_build_xml.return_value = ("<context></context>", [])
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
        message = _make_thread_message(parent_id=789)

        await bot.on_message(message)

        mock_resolve.assert_called_once()
        call_kwargs = mock_resolve.call_args.kwargs
        context: ScopeContext = call_kwargs["context"]
        assert context.channel_id == "789", "should use parent_id, not thread_id"

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session()
        mock_build_xml.return_value = ("<context></context>", [])
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
        message = _make_thread_message()

        await bot.on_message(message)

        # resolve_config is called on every turn
        mock_resolve.assert_called_once()
        mock_run_turn.assert_called_once()

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session()
        mock_build_xml.return_value = ("<context></context>", [])
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
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
        bot = make_bot(runtime)

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

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-auto-archive")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
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
    """The mention error boundary renders plain copy."""

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_turn_error_message_omits_trace_ids(
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
        channel/thread uses plain copy, with the trace id retained in logs."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = ma_session(id="sess-err")
        mock_run_turn.side_effect = _anthropic.APIConnectionError(request=MagicMock())
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
        message = _make_channel_message()
        card = MagicMock(spec=discord.Message)
        card.id = 4242
        card.webhook_id = None
        card.edit = AsyncMock(return_value=card)
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.send = AsyncMock(return_value=card)
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        # The error lands on the turn's own card in its thread, not the channel.
        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        error_text: str = card.edit.call_args.kwargs["content"]
        assert "rid:" not in error_text, (
            f"error boundary must not publish trace ids; got: {error_text!r}"
        )

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        """A connection error must offer a plain retry instruction."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = ma_session(id="sess-err2")
        mock_run_turn.side_effect = _anthropic.APIConnectionError(request=MagicMock())
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
        message = _make_channel_message()
        card = MagicMock(spec=discord.Message)
        card.id = 4242
        card.webhook_id = None
        card.edit = AsyncMock(return_value=card)
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9998
        mock_thread.send = AsyncMock(return_value=card)
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        # The error lands on the turn's own card in its thread, not the channel.
        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        error_text: str = card.edit.call_args.kwargs["content"]
        assert "couldn't reach Claude" in error_text, (
            f"the connection failure should have a plain retry instruction; got: {error_text!r}"
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
        bot = make_bot(runtime)

        # Stub remaining Cogs via sys.modules to keep the test merge-order-independent.
        mock_help_cog = MagicMock()
        mock_here_cog = MagicMock()
        mock_agent_setup_cog = MagicMock()
        mock_routines_cog = MagicMock()
        mock_billing_cog = MagicMock()
        mock_privacy_cog = MagicMock()
        mock_memory_cog = MagicMock()
        mock_github_cog = MagicMock()

        help_mod = types.ModuleType("daimon.adapters.discord.commands.help")
        help_mod.HelpCog = mock_help_cog  # type: ignore[attr-defined]
        here_mod = types.ModuleType("daimon.adapters.discord.commands.here")
        here_mod.HereCog = mock_here_cog  # type: ignore[attr-defined]
        agent_setup_mod = types.ModuleType("daimon.adapters.discord.commands.agent_setup")
        agent_setup_mod.AgentSetupCog = mock_agent_setup_cog  # type: ignore[attr-defined]
        routines_mod = types.ModuleType("daimon.adapters.discord.commands.routines")
        routines_mod.RoutinesCog = mock_routines_cog  # type: ignore[attr-defined]
        billing_mod = types.ModuleType("daimon.adapters.discord.commands.billing")
        billing_mod.BillingCog = mock_billing_cog  # type: ignore[attr-defined]
        privacy_mod = types.ModuleType("daimon.adapters.discord.commands.privacy")
        privacy_mod.PrivacyCog = mock_privacy_cog  # type: ignore[attr-defined]
        memory_mod = types.ModuleType("daimon.adapters.discord.commands.memory")
        memory_mod.MemoryCog = mock_memory_cog  # type: ignore[attr-defined]
        github_mod = types.ModuleType("daimon.adapters.discord.commands.github")
        github_mod.GitHubCog = mock_github_cog  # type: ignore[attr-defined]
        mock_feedback_reaction_cog = MagicMock()
        feedback_reactions_mod = types.ModuleType("daimon.adapters.discord.feedback_reactions")
        feedback_reactions_mod.FeedbackReactionCog = mock_feedback_reaction_cog  # type: ignore[attr-defined]

        # Imported BEFORE patch.dict: it drops any module first imported inside
        # the block when it exits, so importing DirectMessageCog afterwards would
        # load a second copy whose class the bot's instance isn't an instance of.
        from daimon.adapters.discord.commands.direct_messages import DirectMessageCog

        add_cog_calls: list[object] = []

        async def tracking_add_cog(cog: object, **kwargs: object) -> None:
            add_cog_calls.append(cog)

        bot.add_cog = tracking_add_cog  # type: ignore[assignment]
        bot.start_orphan_recovery = MagicMock()  # type: ignore[method-assign]  # the boot sweep is not under test

        with patch.dict(
            "sys.modules",
            {
                "daimon.adapters.discord.commands.help": help_mod,
                "daimon.adapters.discord.commands.here": here_mod,
                "daimon.adapters.discord.commands.agent_setup": agent_setup_mod,
                "daimon.adapters.discord.commands.routines": routines_mod,
                "daimon.adapters.discord.commands.billing": billing_mod,
                "daimon.adapters.discord.commands.privacy": privacy_mod,
                "daimon.adapters.discord.commands.memory": memory_mod,
                "daimon.adapters.discord.commands.github": github_mod,
                "daimon.adapters.discord.feedback_reactions": feedback_reactions_mod,
            },
        ):
            await bot.setup_hook()

        assert len(add_cog_calls) == 10, "setup_hook should add exactly 10 Cogs"
        assert sum(isinstance(cog, DirectMessageCog) for cog in add_cog_calls) == 1
        mock_github_cog.assert_called_once_with(bot)
        mock_help_cog.assert_called_once_with(bot)
        mock_here_cog.assert_called_once_with(bot)
        mock_agent_setup_cog.assert_called_once_with(bot)
        mock_routines_cog.assert_called_once_with(bot)
        mock_billing_cog.assert_called_once_with(bot)
        mock_privacy_cog.assert_called_once_with(bot)
        mock_memory_cog.assert_called_once_with(bot)
        mock_github_cog.assert_called_once_with(bot)
        mock_feedback_reaction_cog.assert_called_once_with(bot)


class TestInvokerAccessPolicy:
    """SYS-047: the tenant's invoker allowlist refuses at admission with a notice."""

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_mention_from_a_user_outside_the_allowlist_is_refused_with_a_notice(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        await set_access_policy(
            db_session, tenant_id=tenant.id, policy=TenantAccessPolicy(invoker_user_ids=("999",))
        )
        await db_session.commit()

        bot = make_bot(_make_runtime(tenant.id, db_session_factory))
        message = _make_channel_message(author_id=111)

        await bot.on_message(message)

        mock_resolve.assert_not_called()
        mock_find_agent.assert_not_called()
        mock_create_session.assert_not_called()
        mock_run_turn.assert_not_called()
        message.create_thread.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        message.channel.send.assert_called_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        sent_text: str = message.channel.send.call_args[0][0]  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]
        assert "can start a turn" in sent_text, sent_text

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_mention_from_an_allowlisted_user_passes_the_policy(
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
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        await set_access_policy(
            db_session, tenant_id=tenant.id, policy=TenantAccessPolicy(invoker_user_ids=("111",))
        )
        await db_session.commit()
        mock_resolve.return_value = _stub_resolved_config()
        mock_is_over_cap.return_value = True

        bot = make_bot(_make_runtime(tenant.id, db_session_factory))
        message = _make_channel_message(author_id=111)

        await bot.on_message(message)

        # Past the policy: the config cascade ran, and the cap gate is what stopped it.
        mock_resolve.assert_awaited_once()
        mock_is_over_cap.assert_awaited_once()


class TestProtectedChannelAdmission:
    """SYS-048: a mention in a protected channel gets no thread, no reply, no upload."""

    @pytest.mark.parametrize(
        ("policy", "in_thread"),
        [
            (TenantAccessPolicy(protected_channel_ids=("789",)), False),
            (TenantAccessPolicy(protected_channel_ids=("789",)), True),
            (TenantAccessPolicy(protected_category_ids=("4242",)), False),
        ],
        ids=["protected-channel", "thread-under-protected", "protected-category"],
    )
    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_mention_in_a_protected_channel_is_dropped_without_posting(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        policy: TenantAccessPolicy,
        in_thread: bool,
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
        await db_session.commit()

        bot = make_bot(_make_runtime(tenant.id, db_session_factory))
        if in_thread:
            message = _make_thread_message(parent_id=789)
            message.channel.parent.category_id = None  # pyright: ignore[reportAttributeAccessIssue]
            posts = message.channel.send  # pyright: ignore[reportAttributeAccessIssue]
        else:
            message = _make_channel_message(channel_id=789)
            message.channel.category_id = 4242  # pyright: ignore[reportAttributeAccessIssue]
            posts = message.channel.send  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        mock_resolve.assert_not_called()
        mock_find_agent.assert_not_called()
        mock_create_session.assert_not_called()
        mock_run_turn.assert_not_called()
        posts.assert_not_called()  # pyright: ignore[reportUnknownMemberType]
        if not in_thread:
            message.create_thread.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]


class TestProtectedChannelSilence:
    """SYS-048: nothing at all is posted for a turn in a protected channel --
    not the invoker refusal, not the capacity notice."""

    async def _bot_for(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        policy: TenantAccessPolicy,
    ) -> tuple[DaimonBot, uuid.UUID]:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
        await db_session.commit()
        return make_bot(_make_runtime(tenant.id, db_session_factory)), tenant.id

    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_a_guest_mention_in_a_protected_channel_gets_no_refusal_notice(
        self,
        mock_resolve: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        bot, _ = await self._bot_for(
            db_session,
            db_session_factory,
            TenantAccessPolicy(invoker_user_ids=("999",), protected_channel_ids=("789",)),
        )
        message = _make_channel_message(channel_id=789, author_id=111)
        message.channel.category_id = None  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        mock_resolve.assert_not_called()
        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        message.create_thread.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]

    async def test_the_capacity_notice_is_not_posted_into_a_protected_channel(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        bot, tenant_id = await self._bot_for(
            db_session, db_session_factory, TenantAccessPolicy(protected_channel_ids=("789",))
        )
        # Saturate the cap with no queue room, so the plain notice is due.
        bot.turn_queue.max_queued_per_tenant = 0
        for _ in range(100):
            bot.turn_queue.claim(tenant_id)
        message = _make_channel_message(channel_id=789)
        message.channel.category_id = None  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]

    async def test_global_capacity_notice_is_not_posted_into_a_protected_channel(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        bot, _ = await self._bot_for(
            db_session, db_session_factory, TenantAccessPolicy(protected_channel_ids=("789",))
        )
        assert bot.runtime.settings.discord is not None
        bot.turn_queue.global_cap = 1
        bot.turn_queue.max_queued = 0
        bot.turn_queue.claim(uuid.uuid4())
        message = _make_channel_message(channel_id=789)
        message.channel.category_id = None  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        assert bot.turn_queue.in_flight() == 1

    async def test_global_capacity_notice_is_not_posted_into_a_protected_thread(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        bot, _ = await self._bot_for(
            db_session, db_session_factory, TenantAccessPolicy(protected_channel_ids=("789",))
        )
        assert bot.runtime.settings.discord is not None
        bot.turn_queue.global_cap = 1
        bot.turn_queue.max_queued = 0
        bot.turn_queue.claim(uuid.uuid4())
        message = _make_thread_message(parent_id=789)

        await bot.on_message(message)

        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        assert bot.turn_queue.in_flight() == 1

    @pytest.mark.parametrize("fetch_fails", [False, True], ids=["fetched", "fetch-failed"])
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_an_uncached_thread_parent_is_fetched_before_judging_its_category(
        self,
        mock_resolve: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        fetch_fails: bool,
    ) -> None:
        """The parent's category is what's protected. An uncached parent is
        fetched; if the fetch fails, a category policy fails closed."""
        bot, _ = await self._bot_for(
            db_session, db_session_factory, TenantAccessPolicy(protected_category_ids=("4242",))
        )
        message = _make_thread_message(parent_id=789)
        thread = message.channel
        thread.parent = None  # pyright: ignore[reportAttributeAccessIssue]
        parent = MagicMock()
        parent.category_id = 4242
        thread.guild = MagicMock()  # pyright: ignore[reportAttributeAccessIssue]
        thread.guild.fetch_channel = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=discord.NotFound(MagicMock(status=404), "gone") if fetch_fails else None,
            return_value=parent,
        )

        await bot.on_message(message)

        thread.guild.fetch_channel.assert_awaited_once_with(789)  # pyright: ignore[reportAttributeAccessIssue]
        mock_resolve.assert_not_called()
        thread.send.assert_not_called()  # pyright: ignore[reportAttributeAccessIssue]


class TestProtectedChannelBeforeAnyNotice:
    """SYS-048 round 4: silence holds before readiness notices and when the
    policy can't be read; the category fetch happens only when needed."""

    @pytest.mark.parametrize(
        ("status", "archive"),
        [("pending", False), ("failed", False), ("ready", True)],
        ids=["pending", "failed", "archived"],
    )
    async def test_no_setup_notice_in_a_protected_channel_of_an_unready_tenant(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        status: str,
        archive: bool,
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await set_access_policy(
            db_session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(protected_channel_ids=("789",)),
        )
        await db_session.commit()
        await set_provision_status(
            db_session_factory, tenant_id=tenant.id, status=status, archive=archive
        )
        bot = make_bot(_make_runtime(tenant.id, db_session_factory))
        message = _make_channel_message(channel_id=789)

        with patch.object(bot, "_ensure_provisioning", new=AsyncMock()) as ensure:
            await bot.on_message(message)

        ensure.assert_not_awaited()
        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]

    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_a_failed_policy_read_stays_silent(
        self,
        mock_resolve: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A DB or pool failure while reading protection posts nothing -- not
        even an error -- into a channel whose safety is unknown."""
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        await db_session.commit()
        bot = make_bot(_make_runtime(tenant.id, db_session_factory))
        message = _make_channel_message(channel_id=789)

        with patch(
            "daimon.core.turn.protection.load_access_policy",
            new=AsyncMock(side_effect=OperationalError("SELECT", {}, Exception("pool gone"))),
        ):
            await bot.on_message(message)

        mock_resolve.assert_not_called()
        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        message.create_thread.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]

    @pytest.mark.parametrize(
        ("policy", "fetches"),
        [
            (None, 0),
            (TenantAccessPolicy(protected_channel_ids=("elsewhere",)), 0),
            (TenantAccessPolicy(protected_category_ids=("other-cat",)), 1),
        ],
        ids=["no-policy", "no-category-policy", "category-policy-fetches-once"],
    )
    @patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_an_uncached_parent_is_fetched_only_for_a_category_policy_and_once(
        self,
        mock_resolve: AsyncMock,
        mock_is_over_cap: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        policy: TenantAccessPolicy | None,
        fetches: int,
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        if policy is not None:
            await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
        await db_session.commit()
        mock_resolve.return_value = _stub_resolved_config()
        mock_is_over_cap.return_value = True  # stop right after admission's gates
        bot = make_bot(_make_runtime(tenant.id, db_session_factory))
        message = _make_thread_message(parent_id=789)
        thread = message.channel
        parent = MagicMock(spec=discord.TextChannel)
        parent.category_id = 4242
        cached: dict[str, object] = {}
        thread.guild = MagicMock()  # pyright: ignore[reportAttributeAccessIssue]
        thread.parent = None  # pyright: ignore[reportAttributeAccessIssue]
        thread.guild.fetch_channel = AsyncMock(return_value=parent)  # pyright: ignore[reportAttributeAccessIssue]

        def _cache(channel: object) -> None:
            cached["parent"] = channel
            thread.parent = channel  # pyright: ignore[reportAttributeAccessIssue]  # what guild._add_channel makes visible

        thread.guild._add_channel = MagicMock(side_effect=_cache)  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        assert thread.guild.fetch_channel.await_count == fetches, (  # pyright: ignore[reportAttributeAccessIssue]
            "fetch only when a category is protected, and never twice for one turn"
        )
        mock_resolve.assert_awaited_once()  # admitted past the protection gate


class TestProtectedChannelWhateverFails:
    """SYS-048 round 5: the may-post state is decided before anything else in
    on_message; whatever fails afterwards -- or while deciding it -- nothing
    is posted into a protected channel. A control proves the same failure
    still reaches an unprotected channel."""

    @pytest.mark.parametrize(
        "failure",
        [
            "liveness_read",
            "policy_read",
            "category_fetch_parent_protected",
            "category_fetch_category_policy",
            "provisioning",
        ],
    )
    async def test_nothing_is_posted_into_a_protected_target(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        failure: str,
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        policy = (
            TenantAccessPolicy(protected_category_ids=("4242",))
            if failure == "category_fetch_category_policy"
            else TenantAccessPolicy(
                protected_channel_ids=("789",), protected_category_ids=("4242",)
            )
        )
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
        await db_session.commit()
        if failure == "provisioning":
            await set_provision_status(db_session_factory, tenant_id=tenant.id, status="pending")
        bot = make_bot(_make_runtime(tenant.id, db_session_factory))
        db_error = OperationalError("SELECT", {}, Exception("pool gone"))

        if failure.startswith("category_fetch"):
            message = _make_thread_message(parent_id=789)
            posts = message.channel.send  # pyright: ignore[reportAttributeAccessIssue]
            message.channel.parent = None  # pyright: ignore[reportAttributeAccessIssue]
            message.channel.guild = MagicMock()  # pyright: ignore[reportAttributeAccessIssue]
            message.channel.guild.fetch_channel = AsyncMock(side_effect=OSError("reset"))  # pyright: ignore[reportAttributeAccessIssue]
        else:
            message = _make_channel_message(channel_id=789)
            message.channel.category_id = None  # pyright: ignore[reportAttributeAccessIssue]
            posts = message.channel.send  # pyright: ignore[reportAttributeAccessIssue]

        breakage = {
            "liveness_read": patch(
                "daimon.adapters.discord.bot.get_tenant_liveness",
                new=AsyncMock(side_effect=db_error),
            ),
            "policy_read": patch(
                "daimon.core.turn.protection.load_access_policy",
                new=AsyncMock(side_effect=db_error),
            ),
            "provisioning": patch.object(
                bot, "_ensure_provisioning", new=AsyncMock(side_effect=db_error)
            ),
        }.get(failure, contextlib.nullcontext())
        with breakage:
            await bot.on_message(message)

        posts.assert_not_called()  # pyright: ignore[reportUnknownMemberType]
        if not failure.startswith("category_fetch"):
            message.create_thread.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        if failure == "category_fetch_parent_protected":
            message.channel.guild.fetch_channel.assert_not_awaited()  # pyright: ignore[reportAttributeAccessIssue]

    async def test_an_unprotected_channel_still_gets_the_prologue_error(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        await db_session.commit()
        bot = make_bot(_make_runtime(tenant.id, db_session_factory))
        message = _make_channel_message(channel_id=789)
        message.channel.category_id = None  # pyright: ignore[reportAttributeAccessIssue]

        with patch(
            "daimon.adapters.discord.bot.get_tenant_liveness",
            new=AsyncMock(side_effect=OperationalError("SELECT", {}, Exception("pool gone"))),
        ):
            await bot.on_message(message)

        message.channel.send.assert_called_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]


class TestContinuationMayPost:
    """The bot hands its continuation dispatcher the access policy's may-post decision."""

    @pytest.mark.parametrize(
        ("policy", "fail_read", "expected"),
        [
            (None, False, True),
            (TenantAccessPolicy(protected_channel_ids=("789",)), False, False),
            (None, True, False),
        ],
        ids=["open", "protected-parent", "unknown"],
    )
    async def test_may_post_in_follows_the_policy(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        policy: TenantAccessPolicy | None,
        fail_read: bool,
        expected: bool,
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        if policy is not None:
            await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
        await db_session.commit()
        bot = make_bot(_make_runtime(tenant.id, db_session_factory))
        thread = _make_thread_message(parent_id=789).channel

        breakage = (
            patch(
                "daimon.core.turn.protection.load_access_policy",
                new=AsyncMock(side_effect=OperationalError("SELECT", {}, Exception("gone"))),
            )
            if fail_read
            else contextlib.nullcontext()
        )
        with breakage:
            allowed = await bot._may_post_in(tenant_id=tenant.id, channel=thread)  # pyright: ignore[reportPrivateUsage]

        assert allowed is expected


class TestBillingAdmissionGate:
    """is_over_cap admission gate + billing posture wiring."""

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        bot = make_bot(runtime)
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

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-undercap")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        mock_is_over_cap.assert_awaited_once()
        mock_create_session.assert_called_once()
        mock_run_turn.assert_called_once()

    @patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock)
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
        bot = make_bot(runtime)
        message = _make_channel_message()
        message.guild = None  # DM

        await bot.on_message(message)

        mock_is_over_cap.assert_not_called()

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_billed_record_wired_with_session_model_id(
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
        """billing passed to run_turn is Billed(record=...) with record bound to
        ma_session.id and ma_session.agent.model.id."""
        import functools as _functools

        from daimon.core.turn.posture import Billed

        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)

        mock_resolve.return_value = _stub_resolved_config()
        mock_is_over_cap.return_value = False
        mock_create_session.return_value = ma_session(id="sess-usage")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
        message = _make_channel_message()
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 9999
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

        await bot.on_message(message)

        mock_run_turn.assert_called_once()
        call_kwargs = mock_run_turn.call_args.kwargs
        billing = call_kwargs["billing"]
        assert isinstance(billing, Billed), "billing should be Billed for a Discord turn"
        record = billing.record
        assert isinstance(record, _functools.partial), (
            "billing.record should be a functools.partial"
        )
        bound = record.keywords
        assert bound["tenant_id"] == tenant.id, "usage recording must bind tenant_id"
        assert bound["platform_user_id"] == "111", "platform_user_id should be bound"
        assert bound["managed_session_id"] == "sess-usage", (
            "managed_session_id should be ma_session.id"
        )
        assert bound["model_id"] == DEFAULT_MODEL_ID, "model_id should be ma_session.agent.model.id"


class TestResolverSelfHeal:
    """Real ma_resolver runs end-to-end; archived cached id self-heals to live tag-matched id."""

    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        from daimon.core.ma_resolver import new_resolver_cache
        from daimon.testing.ma import (
            MARouter,
            build_fake_anthropic,
            list_response,
        )

        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        await db_session.commit()

        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = ma_session(id="sess-heal")

        live_agent = ma_agent(id="ag_live", tenant_id=tenant.id).model_dump(mode="json")
        live_env = ma_environment(id="env_live", tenant_id=tenant.id).model_dump(mode="json")

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
        discord_settings.thread_open_notice_after_s = 3.0
        settings.discord = discord_settings
        settings.thread_naming = ThreadNamingSettings(enabled=False)
        resolver_cache = new_resolver_cache()
        deployment_default = DeploymentDefault()
        runtime = DiscordRuntime(
            settings=settings,
            anthropic=anthropic,
            sessionmaker=db_session_factory,
            notebook_rate_limiter=RateLimiter(max_requests=999),
            billing_config=None,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            turn_deps=_make_turn_deps(
                settings,
                anthropic,
                db_session_factory,
                resolver_cache=resolver_cache,
                deployment_default=deployment_default,
            ),
        )

        bot = make_bot(runtime)
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

    @patch("daimon.core.turn.admission.reconcile_tenant_defaults", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.ma_resolver import new_resolver_cache
        from daimon.testing.ma import (
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
        mock_create_session.return_value = ma_session(id="sess-miss-heal")

        live_agent = ma_agent(id="ag_miss_live", tenant_id=tenant.id).model_dump(mode="json")
        live_env = ma_environment(id="env_miss_live", tenant_id=tenant.id).model_dump(mode="json")

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
        discord_settings.thread_open_notice_after_s = 3.0
        settings.discord = discord_settings
        settings.thread_naming = ThreadNamingSettings(enabled=False)
        resolver_cache = new_resolver_cache()
        deployment_default = DeploymentDefault()
        runtime = DiscordRuntime(
            settings=settings,
            anthropic=anthropic,
            sessionmaker=db_session_factory,
            notebook_rate_limiter=RateLimiter(max_requests=999),
            billing_config=None,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            turn_deps=_make_turn_deps(
                settings,
                anthropic,
                db_session_factory,
                resolver_cache=resolver_cache,
                deployment_default=deployment_default,
            ),
        )

        bot = make_bot(runtime)
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

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-attach")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"

        runtime = _make_runtime(tenant.id, db_session_factory)
        bot = make_bot(runtime)
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
        assert "[attachment] `x.csv`" in user_msg, (
            "data attachment CDN-URL prefix must be prepended to the user message"
        )
        assert "https://cdn.discord/x.csv" in user_msg, "the signed CDN URL must be surfaced"
        # Channel mentions now produce a <channel_context> envelope; trigger content appears
        # in <user_query> at the end, not as raw message.content
        assert "<channel_context" in user_msg, (
            "channel mention wraps context in <channel_context> envelope"
        )
        assert "hello" in user_msg, "trigger content must be preserved in user_query"

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id="sess-image-url")
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
        bot = make_bot(runtime)
        message = _make_channel_message()
        message.attachments = [cast(discord.Attachment, attachment)]
        mock_thread = MagicMock(spec=discord.Thread)
        mock_thread.id = 5053
        mock_thread.send = AsyncMock()
        message.create_thread.return_value = mock_thread

        await bot.on_message(message)

        mock_run_turn.assert_called_once()
        user_msg: str = mock_run_turn.call_args.kwargs["user_message"]
        assert "[attachment] image `chart.png`" in user_msg, (
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

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_delta_xml", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
            snapshot = _snapshot_of(ma_session(id=existing_session_id))
            await create_thread_session(
                seed_session,
                ma_agent_id="ag_test",
                tenant_id=tenant.id,
                platform="discord",
                thread_id="5555",  # matches _make_thread_message default thread_id
                account_id=principal.account_id,
                ma_session_id=existing_session_id,
                watermark_message_id=watermark_msg_id,
                effective_config=snapshot,
                identity_fingerprint=fingerprint_identity(snapshot),
                mutable_fingerprint=fingerprint_mutable(snapshot),
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
        bot = make_bot(runtime)
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

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
        mock_create_session.return_value = ma_session(id=new_session_id)
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
        bot = make_bot(runtime)
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

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
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
            snapshot = _snapshot_of(ma_session(id=old_session_id))
            await create_thread_session(
                seed_session,
                ma_agent_id="ag_test",
                tenant_id=tenant.id,
                platform="discord",
                thread_id="5557",
                account_id=principal.account_id,
                ma_session_id=old_session_id,
                watermark_message_id="100",
                effective_config=snapshot,
                identity_fingerprint=fingerprint_identity(snapshot),
                mutable_fingerprint=fingerprint_mutable(snapshot),
            )
            await seed_session.commit()

        new_session_id = "sesn_new_recreated_001"
        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = ma_session(id=new_session_id)
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
        bot = make_bot(runtime)
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


class TestUnpromptedAdmission:
    """An unprompted (organic thread participation) turn that fails admission
    posts nothing: the notice a mention earns would otherwise be reposted on
    every quiet burst in the thread."""

    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_missing_config_is_silent_when_unprompted(
        self,
        mock_resolve: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        mock_resolve.return_value = _stub_resolved_config(agent_name=None, environment_name=None)
        bot = make_bot(_make_runtime(tenant.id, db_session_factory))
        message = _make_thread_message(content="what about the residuals?")

        await bot._orchestrate(message, "123456", tenant.id, unprompted=True)  # pyright: ignore[reportPrivateUsage]

        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_depleted_balance_is_silent_when_unprompted_but_not_for_a_mention(
        self,
        mock_resolve: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # No ledger entry: the balance gate denies.
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        mock_resolve.return_value = _stub_resolved_config()
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        bot = make_bot(_make_runtime(tenant.id, db_session_factory))

        unprompted = _make_thread_message(content="and the priors?")
        await bot._orchestrate(unprompted, "123456", tenant.id, unprompted=True)  # pyright: ignore[reportPrivateUsage]
        unprompted.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]

        mention = _make_thread_message()
        await bot._orchestrate(mention, "123456", tenant.id)  # pyright: ignore[reportPrivateUsage]
        mention.channel.send.assert_called_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        sent: str = mention.channel.send.call_args[0][0]  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]
        assert "credit" in sent.lower(), "a mention still gets the depleted-credit notice"

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_exhausted_channel_budget_is_silent_when_unprompted_but_not_for_a_mention(
        self,
        mock_resolve: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await tenant_ledger.insert_entry(
            db_session,
            tenant_id=tenant.id,
            delta_usd=Decimal("10"),
            reason="trial",
            idempotency_key=f"trial:{tenant.id}",
        )
        await make_channel_budget(db_session, tenant=tenant, channel_id="789", limit_usd=Decimal(0))
        await db_session.commit()
        mock_resolve.return_value = _stub_resolved_config()
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        bot = make_bot(_make_runtime(tenant.id, db_session_factory))

        unprompted = _make_thread_message(content="and the priors?")
        await bot._orchestrate(unprompted, "123456", tenant.id, unprompted=True)  # pyright: ignore[reportPrivateUsage]
        unprompted.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]

        mention = _make_thread_message()
        await bot._orchestrate(mention, "123456", tenant.id)  # pyright: ignore[reportPrivateUsage]
        mention.channel.send.assert_called_once()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        sent: str = mention.channel.send.call_args[0][0]  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]
        assert "spending budget" in sent, "a mention gets the channel budget notice"


class TestOverCapQueue:
    """Over a cap a mention posts the ordinary card and waits behind it."""

    async def _queued_turn(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> tuple[DaimonBot, TurnTicket, MagicMock, MagicMock, asyncio.Task[None]]:
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        bot = make_bot(_make_runtime(tenant.id, db_session_factory))
        bot.turn_queue.global_cap = 1
        held = bot.turn_queue.claim(uuid.uuid4())  # another guild's turn holds the only slot
        message = _make_channel_message()
        card = MagicMock(spec=discord.Message)
        card.id = 4242
        card.webhook_id = None
        card.edit = AsyncMock(return_value=card)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 9999
        thread.send = AsyncMock(return_value=card)
        message.create_thread = AsyncMock(return_value=thread)  # pyright: ignore[reportAttributeAccessIssue]
        turn = asyncio.create_task(bot.on_message(message))
        for _ in range(200):
            if thread.send.await_count:
                break
            await asyncio.sleep(0.01)
        assert thread.send.await_count, "the card is posted while the turn waits"
        assert bot.turn_queue.depth() == 1
        message.channel.send.assert_not_called()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        return bot, held, thread, card, turn

    @staticmethod
    def _card_texts(card: MagicMock) -> list[str]:
        return [str(c.kwargs.get("content")) for c in card.edit.await_args_list]

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_a_queued_mention_shows_the_ordinary_card_and_starts_on_a_free_slot(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_run_turn: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        mock_resolve.return_value = _stub_resolved_config()
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        mock_create_session.return_value = ma_session(id="sess-queued")
        bot, held, thread, _card, turn = await self._queued_turn(db_session, db_session_factory)

        embed = cast("list[discord.Embed]", thread.send.await_args.kwargs["embeds"])[0]
        assert embed.title == "Working on it…", "the same card as any turn; no queue words"
        assert "slot" not in (embed.description or "").lower()
        await asyncio.sleep(0.05)
        mock_create_session.assert_not_called()

        held.release()  # the other guild's turn ends: the queued one starts
        await asyncio.wait_for(turn, 5)
        mock_create_session.assert_awaited_once()
        assert bot.turn_queue.depth() == 0
        assert bot.turn_queue.in_flight() == 0

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_stop_while_queued_ends_the_card_as_stopped_and_never_starts(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        mock_resolve.return_value = _stub_resolved_config()
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        bot, held, thread, card, turn = await self._queued_turn(db_session, db_session_factory)

        view = thread.send.await_args.kwargs["view"]
        assert isinstance(view, CancelView)
        view._cancel.set()  # pyright: ignore[reportPrivateUsage]  # the Stop click
        await asyncio.wait_for(turn, 5)

        assert self._card_texts(card) == ["Stopped.\nSend a message to start again."]
        assert bot.turn_queue.depth() == 0
        held.release()
        await asyncio.sleep(0.05)
        mock_create_session.assert_not_called()
        assert bot.turn_queue.in_flight() == 0

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_the_max_wait_ends_the_card_with_the_ordinary_error(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        mock_resolve.return_value = _stub_resolved_config()
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
        await _setup_workspace_and_config(db_session, tenant.id)
        bot = make_bot(_make_runtime(tenant.id, db_session_factory))
        bot.turn_queue.global_cap = 1
        bot.turn_queue.max_wait_s = 0.05
        bot.turn_queue.claim(uuid.uuid4())
        message = _make_channel_message()
        card = MagicMock(spec=discord.Message)
        card.id = 4243
        card.edit = AsyncMock(return_value=card)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 9998
        thread.send = AsyncMock(return_value=card)
        message.create_thread = AsyncMock(return_value=thread)  # pyright: ignore[reportAttributeAccessIssue]

        await asyncio.wait_for(bot.on_message(message), 5)

        assert self._card_texts(card) == ["Something went wrong. Mention me to try again."]
        mock_create_session.assert_not_called()
        assert bot.turn_queue.depth() == 0
        assert bot.turn_queue.in_flight() == 1, "only the other guild's turn holds a slot"

    @patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
    @patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
    @patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
    async def test_a_restart_retires_a_queued_card_like_any_orphan(
        self,
        mock_resolve: AsyncMock,
        mock_create_session: AsyncMock,
        mock_find_env: AsyncMock,
        mock_find_agent: AsyncMock,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        mock_resolve.return_value = _stub_resolved_config()
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        bot, _held, _thread, card, turn = await self._queued_turn(db_session, db_session_factory)
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id="123456")

        # The process dies with the turn still queued; a new one boots.
        restarted = make_bot(_make_runtime(tenant_id, db_session_factory))
        restarted_thread = MagicMock(spec=discord.Thread)
        restarted_thread.fetch_message = AsyncMock(return_value=card)
        restarted.get_channel = MagicMock(return_value=restarted_thread)  # pyright: ignore[reportAttributeAccessIssue]
        restarted.wait_until_ready = AsyncMock()  # pyright: ignore[reportAttributeAccessIssue]
        intents: list[TurnCardIntentRow] = []
        for _ in range(100):  # the card's id commits just after its post
            async with db_session_factory() as session:
                intents = await list_recoverable_turn_card_intents(session, platform="discord")
            if intents and intents[0].message_id is not None:
                break
            await asyncio.sleep(0.01)
        (intent,) = intents
        assert intent.message_id == str(card.id), "the queued card is recorded like any other"
        # What the boot sweep runs for each recoverable intent. The card's Stop
        # button carries the intent id; a mock message has no components.
        with patch(
            "daimon.adapters.discord.turn_card_recovery.turn_card_ids_from_message",
            return_value=frozenset({intent.id}),
        ):
            await restarted._reconcile_turn_card_intent(intent)  # pyright: ignore[reportPrivateUsage]

        titles = [
            embed.title
            for call in card.edit.await_args_list
            for embed in cast("list[discord.Embed]", call.kwargs.get("embeds") or [])
        ] + [
            call.kwargs["embed"].title
            for call in card.edit.await_args_list
            if call.kwargs.get("embed") is not None
        ]
        assert "Daimon restarted before this request finished." in titles
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn
        mock_create_session.assert_not_called()
        assert bot.turn_queue.depth() == 0
