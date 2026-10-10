"""Behavioral tests for DaimonBot graceful shutdown drain (Plan 55-03).

Tests cover:
- on_message rejects new mentions while draining (drain gate)
- _drain_and_close awaits in-flight turns and then calls close()
- _drain_and_close sets draining=True then calls close() exactly once
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.tool_confirmation import discord_confirmation_hook
from daimon.core.config import McpSettings
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.tool_safety import ToolCall
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
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # shutdown tests never run a turn
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
