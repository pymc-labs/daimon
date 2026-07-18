"""Behavioral tests for DaimonBot graceful shutdown drain (Plan 55-03).

Tests cover:
- on_message rejects new mentions while draining (drain gate)
- _drain_and_close awaits in-flight turns and then calls close()
- _drain_and_close sets draining=True then calls close() exactly once
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from sqlalchemy.ext.asyncio import async_sessionmaker


def _make_runtime() -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.defaults_root = MagicMock()
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 3
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
    )


def _make_bot() -> DaimonBot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = DaimonBot(runtime=_make_runtime(), intents=intents)
    bot._connection.user = MagicMock(spec=discord.ClientUser)  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.id = 999  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.mentioned_in = MagicMock(return_value=True)  # pyright: ignore[reportPrivateUsage]
    return bot


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
    bot = _make_bot()
    bot.draining = True
    message = _make_mention_message()

    with patch.object(bot, "_handle_mention", new_callable=AsyncMock) as mock_handle:
        await bot.on_message(message)

    assert len(bot._processing) == 0, "no thread should be added to _processing when draining"  # pyright: ignore[reportPrivateUsage]
    mock_handle.assert_not_called(), "draining bot must not invoke _handle_mention"  # pyright: ignore[reportUnusedExpression]


@pytest.mark.asyncio
async def test_drain_sets_flag_then_closes() -> None:
    """_drain_and_close sets draining=True before awaiting and calls close() exactly once."""
    bot = _make_bot()
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
    bot = _make_bot()
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
    bot = _make_bot()
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
