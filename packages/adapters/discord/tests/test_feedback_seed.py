"""Tests for seed_feedback_reactions -- the best-effort seeding helper, and
its wiring into _orchestrate's terminal-success branch.

The unit tests below need no database. The integration tests drive
`DaimonBot._orchestrate` end to end the way `test_orchestration.py` does,
duplicating just enough of that file's setup helpers inline (per this
codebase's testing guideline: don't share fixtures across test modules,
inline what each file needs).
"""

from __future__ import annotations

import types
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from anthropic import AsyncAnthropic
from daimon.adapters.discord.feedback_seed import seed_feedback_reactions
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import McpSettings, ThreadNamingSettings
from daimon.core.errors import TurnError
from daimon.core.ma_resolver import ResolverCache, new_resolver_cache
from daimon.core.message_feedback import THUMBS_DOWN, THUMBS_UP
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores import tenant_ledger
from daimon.core.support_escalation import ESCALATE
from daimon.core.turn.deps import TurnDeps, build_turn_deps
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing import ma_session, resolved_agent_env_router
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import make_bot

# --- unit tests: the helper alone -------------------------------------------


async def test_seed_adds_the_two_vote_emoji_then_the_escalate_emoji() -> None:
    partial = MagicMock()
    partial.add_reaction = AsyncMock()
    channel = MagicMock()
    channel.get_partial_message = MagicMock(return_value=partial)

    await seed_feedback_reactions(channel, message_id="123")

    assert partial.add_reaction.await_count == 3, "both vote emoji and the escalate emoji"
    first_call, second_call, third_call = partial.add_reaction.await_args_list
    assert first_call.args[0] == THUMBS_UP, "the positive emoji must be added first"
    assert second_call.args[0] == THUMBS_DOWN, "the negative emoji must be added second"
    assert third_call.args[0] == ESCALATE, (
        "the escalate emoji is seeded LAST so the two vote emoji keep the order "
        "they have always rendered in"
    )


async def test_seed_when_add_reaction_raises_forbidden_returns_normally() -> None:
    partial = MagicMock()
    partial.add_reaction = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "no perms")
    )
    channel = MagicMock()
    channel.get_partial_message = MagicMock(return_value=partial)

    await seed_feedback_reactions(channel, message_id="123")  # must not raise


async def test_seed_when_first_add_reaction_raises_forbidden_returns_normally() -> None:
    partial = MagicMock()
    partial.add_reaction = AsyncMock(
        side_effect=[discord.Forbidden(MagicMock(status=403), "no perms"), None]
    )
    channel = MagicMock()
    channel.get_partial_message = MagicMock(return_value=partial)

    await seed_feedback_reactions(channel, message_id="123")  # must not raise


async def test_seed_when_add_reaction_raises_a_connection_error_returns_normally() -> None:
    """An aiohttp/OSError surviving discord.py's retry loop is not an HTTPException.

    It runs after the answer was already delivered, so letting it escape would
    post a "turn failed" message under a successful answer.
    """
    partial = MagicMock()
    partial.add_reaction = AsyncMock(side_effect=OSError("connection reset by peer"))
    channel = MagicMock()
    channel.get_partial_message = MagicMock(return_value=partial)

    await seed_feedback_reactions(channel, message_id="123")  # must not raise


async def test_seed_when_add_reaction_raises_on_a_closed_session_returns_normally() -> None:
    """Shutdown drain: aiohttp raises RuntimeError once the session is closed."""
    partial = MagicMock()
    partial.add_reaction = AsyncMock(side_effect=RuntimeError("Session is closed"))
    channel = MagicMock()
    channel.get_partial_message = MagicMock(return_value=partial)

    await seed_feedback_reactions(channel, message_id="123")  # must not raise


async def test_seed_is_a_no_op_for_a_channel_type_without_get_partial_message() -> None:
    channel = MagicMock(spec=[])  # no attributes at all, including get_partial_message

    await seed_feedback_reactions(channel, message_id="123")  # must not raise


# --- integration: wired into _orchestrate's terminal-success branch --------


def _make_turn_deps(
    settings: MagicMock,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    resolver_cache: ResolverCache,
    deployment_default: DeploymentDefault,
) -> TurnDeps:
    return build_turn_deps(
        settings,
        anthropic,
        sessionmaker,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        billing_config=None,
    )


def _make_runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.thread_naming = ThreadNamingSettings(enabled=False)
    settings.billing.markup = Decimal("1.0")
    settings.billing.signup_credit = Decimal("0")
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 100
    settings.discord = discord_settings
    anthropic = build_stub_anthropic(resolved_agent_env_router().dispatch)
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


class _AsyncIter:
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
    message.channel.history = MagicMock(return_value=_AsyncIter([]))
    message.create_thread = AsyncMock()
    message.add_reaction = AsyncMock()
    message.attachments = []
    message.mentions = [types.SimpleNamespace(id=999)]
    return message


def _stub_resolved_config() -> ResolvedConfig:
    return ResolvedConfig(
        agent_name="test-agent",
        agent_name_tier="tenant",
        environment_name="test-env",
        environment_name_tier="tenant",
    )


async def _seed_balance(db_session: AsyncSession, tenant_id: uuid.UUID) -> None:
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant_id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant_id}",
    )
    await db_session.flush()


def _make_seeding_thread() -> MagicMock:
    """A mock_thread whose get_partial_message returns an AsyncMock-backed partial."""
    bot_reply_msg = MagicMock(spec=discord.Message)
    bot_reply_msg.id = 777888999
    mock_thread = MagicMock(spec=discord.Thread)
    mock_thread.id = 9999
    mock_thread.send = AsyncMock(return_value=bot_reply_msg)
    partial = MagicMock()
    partial.add_reaction = AsyncMock()
    mock_thread.get_partial_message = MagicMock(return_value=partial)
    return mock_thread


def _fake_run_turn(final_state: TurnState) -> object:
    """Build a `run_turn` side_effect that actually drives the terminal
    lifecycle hook, the way the real driver does -- a bare `.return_value`
    on the mocked `run_turn` skips the hook entirely, so `was_answered`
    would never flip regardless of what `TurnState` is returned.
    """

    async def _run(*, lifecycle: object, **_kwargs: object) -> TurnState:
        if final_state.error is not None:
            await lifecycle.on_terminal_failure(final_state, final_state.error)  # type: ignore[attr-defined]
        else:
            await lifecycle.on_terminal_success(final_state)  # type: ignore[attr-defined]
        return final_state

    return _run


@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
@patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
async def test_a_real_text_answer_seeds_all_three_emoji_on_the_final_message(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_run_turn: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
    await _seed_balance(db_session, tenant.id)

    mock_resolve.return_value = _stub_resolved_config()
    mock_create_session.return_value = ma_session(id="sess-seed-1")
    mock_run_turn.side_effect = _fake_run_turn(
        TurnState(content=[TextBlock(kind="text", text="the answer")])
    )
    mock_find_agent.return_value = "ag_test"
    mock_find_env.return_value = "env_test"

    runtime = _make_runtime(db_session_factory)
    bot = make_bot(runtime)
    message = _make_channel_message()
    mock_thread = _make_seeding_thread()
    message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

    await bot.on_message(message)

    mock_thread.get_partial_message.assert_called_once_with(777888999)
    assert mock_thread.get_partial_message.return_value.add_reaction.await_count == 3, (
        "an answered turn must seed both vote emoji on its final message"
    )


@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
@patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
async def test_a_cancelled_turn_seeds_nothing(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_run_turn: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="123457")
    await _seed_balance(db_session, tenant.id)

    mock_resolve.return_value = _stub_resolved_config()
    mock_create_session.return_value = ma_session(id="sess-seed-2")
    mock_run_turn.side_effect = _fake_run_turn(
        TurnState()
    )  # no content/error -- cancellation shape
    mock_find_agent.return_value = "ag_test"
    mock_find_env.return_value = "env_test"

    runtime = _make_runtime(db_session_factory)
    bot = make_bot(runtime)
    message = _make_channel_message(guild_id=123457)
    mock_thread = _make_seeding_thread()
    message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

    await bot.on_message(message)

    mock_thread.get_partial_message.assert_not_called()


@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
@patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
async def test_a_turn_that_ends_in_error_seeds_nothing(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_run_turn: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="123458")
    await _seed_balance(db_session, tenant.id)

    mock_resolve.return_value = _stub_resolved_config()
    mock_create_session.return_value = ma_session(id="sess-seed-3")
    mock_run_turn.side_effect = _fake_run_turn(
        TurnState(error=TurnError(kind="reducer_bug", message="boom", cause=RuntimeError("boom")))
    )
    mock_find_agent.return_value = "ag_test"
    mock_find_env.return_value = "env_test"

    runtime = _make_runtime(db_session_factory)
    bot = make_bot(runtime)
    message = _make_channel_message(guild_id=123458)
    mock_thread = _make_seeding_thread()
    message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

    await bot.on_message(message)

    mock_thread.get_partial_message.assert_not_called()


@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
@patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
async def test_seeding_forbidden_still_completes_the_turn_and_writes_the_watermark(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_run_turn: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.stores.identity import get_or_create_platform_principal
    from daimon.core.stores.thread_sessions import get_live_thread_session

    tenant = await make_tenant(db_session, platform="discord", workspace_id="123459")
    await _seed_balance(db_session, tenant.id)
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="111"
    )
    await db_session.commit()

    mock_resolve.return_value = _stub_resolved_config()
    mock_create_session.return_value = ma_session(id="sess-seed-4")
    mock_run_turn.side_effect = _fake_run_turn(
        TurnState(content=[TextBlock(kind="text", text="the answer")])
    )
    mock_find_agent.return_value = "ag_test"
    mock_find_env.return_value = "env_test"

    runtime = _make_runtime(db_session_factory)
    bot = make_bot(runtime)
    message = _make_channel_message(guild_id=123459)
    mock_thread = _make_seeding_thread()
    mock_thread.get_partial_message.return_value.add_reaction = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "no perms")
    )
    message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

    await bot.on_message(message)  # must not raise

    async with db_session_factory() as verify_session:
        row = await get_live_thread_session(
            verify_session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="9999",
            account_id=principal.account_id,
        )
    assert row is not None, "the turn must still complete and persist its session mapping"
    assert row.watermark_message_id == "777888999", (
        "seeding failure must not prevent the watermark write"
    )
