"""A slow thread opening is acknowledged with a reaction, never a channel message."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.bot import (
    THREAD_OPENING_REACTION,
    _open_thread_with_notice,  # pyright: ignore[reportPrivateUsage]
)


def _message() -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.reply = AsyncMock()
    message.add_reaction = AsyncMock()
    message.remove_reaction = AsyncMock()
    message.guild = MagicMock(spec=discord.Guild)
    return message


async def test_slow_creation_reacts_then_clears_the_reaction() -> None:
    message = _message()
    release = asyncio.Event()
    thread = MagicMock(spec=discord.Thread)

    async def opening() -> discord.Thread:
        await release.wait()
        return thread

    pending = asyncio.create_task(_open_thread_with_notice(message, opening(), after_s=0.01))
    await asyncio.sleep(0.03)
    message.add_reaction.assert_awaited_once_with(THREAD_OPENING_REACTION)
    message.remove_reaction.assert_not_awaited()
    assert not pending.done()
    release.set()
    assert await pending is thread
    message.remove_reaction.assert_awaited_once_with(THREAD_OPENING_REACTION, message.guild.me)
    message.reply.assert_not_awaited()


async def test_fast_creation_has_no_reaction() -> None:
    message = _message()
    thread = MagicMock(spec=discord.Thread)

    async def opening() -> discord.Thread:
        return thread

    assert await _open_thread_with_notice(message, opening(), after_s=1) is thread
    message.add_reaction.assert_not_awaited()
    message.remove_reaction.assert_not_awaited()
    message.reply.assert_not_awaited()


async def test_failed_creation_clears_the_reaction_and_propagates() -> None:
    message = _message()

    async def opening() -> discord.Thread:
        await asyncio.sleep(0.02)
        raise RuntimeError("thread creation failed")

    with pytest.raises(RuntimeError, match="thread creation failed"):
        await _open_thread_with_notice(message, opening(), after_s=0)
    message.add_reaction.assert_awaited_once_with(THREAD_OPENING_REACTION)
    message.remove_reaction.assert_awaited_once_with(THREAD_OPENING_REACTION, message.guild.me)
    message.reply.assert_not_awaited()


async def test_reaction_failure_still_opens_the_thread() -> None:
    message = _message()
    message.add_reaction.side_effect = discord.HTTPException(
        MagicMock(status=403), "Missing Access"
    )
    thread = MagicMock(spec=discord.Thread)

    async def opening() -> discord.Thread:
        return thread

    assert await _open_thread_with_notice(message, opening(), after_s=0) is thread
    message.remove_reaction.assert_not_awaited()
    message.reply.assert_not_awaited()
