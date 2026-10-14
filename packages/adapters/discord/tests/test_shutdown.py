"""Behavioral tests for DaimonBot graceful shutdown drain (Plan 55-03).

Tests cover:
- on_message rejects new mentions while draining (drain gate)
- _drain_and_close awaits in-flight turns and then calls close()
- _drain_and_close sets draining=True then calls close() exactly once
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.tool_confirmation import discord_confirmation_hook
from daimon.core.config import McpSettings
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.tool_safety import ToolCall
from daimon.core.turn.running import RunningTurns
from daimon.testing.turn_fakes import deps_for
from sqlalchemy.ext.asyncio import async_sessionmaker

from .harness import make_bot


def _make_runtime() -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.defaults_root = MagicMock()
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 3
    discord_settings.thread_open_notice_after_s = 3.0
    settings.discord = discord_settings
    anthropic = AsyncMock()
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=MagicMock(spec=async_sessionmaker),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=replace(deps_for(), turn_path=None),  # shutdown tests never run a turn
    )


def _make_mention_message(
    *,
    guild_id: int = 123456,
    channel_id: int = 789,
) -> discord.Message:
    message = MagicMock(spec=discord.Message)
    message.content = "<@999> hello"
    message.author = MagicMock()
    message.author.bot = False
    message.author.id = 111
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = guild_id
    message.channel = MagicMock()
    message.channel.__class__ = discord.TextChannel
    message.channel.id = channel_id
    message.channel.send = AsyncMock()
    message.add_reaction = AsyncMock()
    message.mentions = [SimpleNamespace(id=999)]
    return message


@pytest.mark.asyncio
async def test_on_message_rejects_when_draining() -> None:
    """on_message returns early without starting a turn when bot.draining is True.

    The drain gate must prevent new mentions from being admitted to _processing
    and must not invoke _handle_mention.
    """
    bot = make_bot(_make_runtime())
    bot.draining = True
    message = _make_mention_message()

    with patch.object(bot, "_handle_mention", new_callable=AsyncMock) as mock_handle:
        await bot.on_message(message)

    assert len(bot._processing) == 0, "no thread should be added to _processing when draining"  # pyright: ignore[reportPrivateUsage]
    mock_handle.assert_not_called(), "draining bot must not invoke _handle_mention"  # pyright: ignore[reportUnusedExpression]


@pytest.mark.asyncio
async def test_drain_sets_flag_then_closes() -> None:
    """_drain_and_close sets draining=True before awaiting and calls close() exactly once."""
    bot = make_bot(_make_runtime())
    bot.close = AsyncMock()  # type: ignore[method-assign]

    assert bot.draining is False, "draining must start False"
    await bot._drain_and_close()  # pyright: ignore[reportPrivateUsage]

    assert bot.draining is True, "_drain_and_close must set draining=True"
    bot.close.assert_called_once(), "close() must be called exactly once"  # pyright: ignore[reportUnusedExpression]


@pytest.mark.asyncio
async def test_drain_awaits_inflight_then_closes() -> None:
    """_drain_and_close polls _processing until empty, then calls close().

    When a thread id is in _processing it stays there until a background coroutine
    removes it (simulating the in-flight turn completing). The drain must wait
    for the set to empty (within a short test grace window), then call close().
    """
    bot = make_bot(_make_runtime())
    bot.close = AsyncMock()  # type: ignore[method-assign]

    thread_id = 789
    bot._processing.add(thread_id)  # pyright: ignore[reportPrivateUsage]

    async def _remove_after_delay() -> None:
        await asyncio.sleep(0.1)
        bot._processing.discard(thread_id)  # pyright: ignore[reportPrivateUsage]

    removal_task = asyncio.create_task(_remove_after_delay())

    # Patch _DRAIN_GRACE_S to a short window so the test runs fast
    with patch("daimon.adapters.discord.bot._DRAIN_GRACE_S", 2.0):
        await bot._drain_and_close()  # pyright: ignore[reportPrivateUsage]

    await removal_task

    assert bot.draining is True, "_drain_and_close must set draining=True"
    assert len(bot._processing) == 0, "drain should wait until _processing is empty"  # pyright: ignore[reportPrivateUsage]
    bot.close.assert_called_once(), "close() must be called after drain completes"  # pyright: ignore[reportUnusedExpression]


@pytest.mark.asyncio
async def test_drain_proceeds_to_close_on_grace_window_expiry() -> None:
    """_drain_and_close calls close() even when _processing is still non-empty after the grace window.

    A cut turn is acceptable (retryable); the process must not hang past the grace window.
    """
    bot = make_bot(_make_runtime())
    bot.close = AsyncMock()  # type: ignore[method-assign]

    thread_id = 789
    bot._processing.add(  # pyright: ignore[reportPrivateUsage]
        thread_id
    )  # never removed — simulates a hung turn  # pyright: ignore[reportPrivateUsage]

    # Patch _DRAIN_GRACE_S to a very short window so the test completes fast
    with patch("daimon.adapters.discord.bot._DRAIN_GRACE_S", 0.1):
        await bot._drain_and_close()  # pyright: ignore[reportPrivateUsage]

    assert bot.draining is True, "_drain_and_close must set draining=True"
    bot.close.assert_called_once(), "close() must be called even if drain timed out"  # pyright: ignore[reportUnusedExpression]


async def test_drain_waits_for_budget_notices_before_closing() -> None:
    """close() shuts the HTTP session a pending budget notice still needs for its DMs."""
    bot = make_bot(_make_runtime())
    order: list[str] = []

    async def closing() -> None:
        order.append("close")

    async def draining() -> None:
        order.append("notices")

    bot.close = closing  # type: ignore[method-assign]
    with patch("daimon.adapters.discord.bot.drain_budget_notices", draining):
        await bot._drain_and_close()  # pyright: ignore[reportPrivateUsage]

    assert order == ["notices", "close"], "pending notices finish while the client is open"


async def test_drain_retires_live_confirmation_before_client_close() -> None:
    bot = make_bot(_make_runtime())
    edit_started = asyncio.Event()
    finish_edit = asyncio.Event()
    closed = False
    message = MagicMock()

    async def edit_card(**_kwargs: object) -> None:
        assert not closed
        edit_started.set()
        await finish_edit.wait()
        assert not closed

    message.edit = AsyncMock(side_effect=edit_card)
    channel = MagicMock(send=AsyncMock(return_value=message))
    prompt = prompt_for_tool_call(
        ToolCall(tool_use_id="tu_1", server_name="linear", tool_name="create_issue", input={}),
        requester_platform_user_id="111",
        now=datetime.now(UTC),
    )

    async def turn() -> None:
        bot._processing.add(789)  # pyright: ignore[reportPrivateUsage]
        bot._track_processing_task(789)  # pyright: ignore[reportPrivateUsage]
        try:
            await discord_confirmation_hook(channel)(prompt)
        finally:
            bot._release_thread(789)  # pyright: ignore[reportPrivateUsage]

    async def close() -> None:
        nonlocal closed
        assert edit_started.is_set()
        assert finish_edit.is_set()
        assert message.edit.await_count == 1
        closed = True

    bot.close = close  # type: ignore[method-assign]
    turn_task = asyncio.create_task(turn())
    while not channel.send.await_count:
        await asyncio.sleep(0)
    with patch("daimon.adapters.discord.bot._DRAIN_GRACE_S", 0.01):
        drain = asyncio.create_task(bot._drain_and_close())  # pyright: ignore[reportPrivateUsage]
        await asyncio.wait({asyncio.create_task(edit_started.wait())}, timeout=1.0)
        assert not closed
        finish_edit.set()
        await drain
    with contextlib.suppress(asyncio.CancelledError):
        await turn_task
    assert closed


def _bot_with_running_turns() -> tuple[DaimonBot, RunningTurns, AsyncMock]:
    runtime = _make_runtime()
    running = RunningTurns()
    runtime = replace(runtime, turn_deps=replace(runtime.turn_deps, running_turns=running))
    bot = make_bot(runtime)
    close = AsyncMock()
    bot.close = close  # type: ignore[method-assign]
    return bot, running, close


async def test_drain_waits_for_a_turn_that_claims_no_thread() -> None:
    """A turn started outside the mention path (a wizard submit, a DM) holds no
    `_processing` claim; the drain must still count it and wait for it (#16)."""
    bot, running, close = _bot_with_running_turns()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def turn() -> None:
        with running.track():
            entered.set()
            await release.wait()

    turn_task = asyncio.create_task(turn())
    await entered.wait()
    assert not bot._processing  # pyright: ignore[reportPrivateUsage]
    assert bot.turn_queue.in_flight() == 0

    with (
        patch("daimon.adapters.discord.bot._DRAIN_GRACE_S", 5.0),
        patch("daimon.adapters.discord.bot.log") as log,
    ):
        drain = asyncio.create_task(bot._drain_and_close())  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.6)
        assert not drain.done(), "the drain must wait for the running turn"
        close.assert_not_called()
        release.set()
        await asyncio.wait_for(drain, timeout=3.0)

    draining = next(c for c in log.info.call_args_list if c.args == ("discord.draining",))
    assert draining.kwargs["running_turns"] == 1
    assert draining.kwargs["inflight_threads"] == 0
    assert turn_task.done() and not turn_task.cancelled(), "the turn finished on its own"
    close.assert_called_once()


async def test_drain_cancels_a_registered_turn_left_after_the_grace_window() -> None:
    bot, running, close = _bot_with_running_turns()
    entered = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def turn() -> None:
        with running.track():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

    turn_task = asyncio.create_task(turn())
    await entered.wait()
    with patch("daimon.adapters.discord.bot._DRAIN_GRACE_S", 0.1):
        await asyncio.wait_for(bot._drain_and_close(), timeout=5.0)  # pyright: ignore[reportPrivateUsage]

    assert turn_task.cancelled(), "a turn still running after the grace is cancelled"
    assert cleaned_up.is_set(), "its cleanup ran before the client closed"
    assert running.count() == 0
    close.assert_called_once()


@pytest.mark.parametrize("by_drain", [True, False], ids=["drain", "other-cancel"])
async def test_only_the_drains_cancel_keeps_a_turns_marker_for_recovery(by_drain: bool) -> None:
    """A turn's cleanup keeps its thread marker only while unwinding from the
    drain's cancel; any other end of a turn clears it as before."""
    bot, running, _close = _bot_with_running_turns()
    entered = asyncio.Event()
    seen: list[bool] = []

    async def turn() -> None:
        with running.track():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                seen.append(running.is_drain_cancel())

    task = asyncio.create_task(turn())
    await entered.wait()
    assert not running.is_drain_cancel()
    if by_drain:
        with patch("daimon.adapters.discord.bot._DRAIN_GRACE_S", 0.05):
            await asyncio.wait_for(bot._drain_and_close(), timeout=5.0)  # pyright: ignore[reportPrivateUsage]
    else:
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert seen == [by_drain]
