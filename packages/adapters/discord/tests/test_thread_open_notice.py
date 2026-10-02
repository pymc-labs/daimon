"""Opening notices cover Discord's internal wait through thread-create 429s."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.bot import (
    _open_thread_with_notice,  # pyright: ignore[reportPrivateUsage]
)


def _message() -> tuple[discord.Message, AsyncMock]:
    message = MagicMock(spec=discord.Message)
    notice = MagicMock(spec=discord.Message)
    notice.edit = AsyncMock()
    message.reply = AsyncMock(return_value=notice)
    return message, notice.edit


async def test_slow_creation_posts_notice_then_links_thread() -> None:
    message, edit = _message()
    release = asyncio.Event()
    thread = MagicMock(spec=discord.Thread)
    thread.id = 123

    async def opening() -> discord.Thread:
        await release.wait()
        return thread

    pending = asyncio.create_task(
        _open_thread_with_notice(message, opening(), guild_id="456", after_s=0.01)
    )
    await asyncio.sleep(0.03)
    assert isinstance(message.reply, AsyncMock)
    message.reply.assert_awaited_once()
    assert not pending.done()
    release.set()
    assert await pending is thread
    assert isinstance(message.reply, AsyncMock)
    message.reply.assert_awaited_once()
    edit.assert_awaited_once_with(
        content="Your chat is ready: https://discord.com/channels/456/123"
    )


async def test_fast_creation_has_no_notice() -> None:
    message, edit = _message()
    thread = MagicMock(spec=discord.Thread)

    async def opening() -> discord.Thread:
        return thread

    assert await _open_thread_with_notice(message, opening(), guild_id="456", after_s=1) is thread
    assert isinstance(message.reply, AsyncMock)
    message.reply.assert_not_awaited()
    edit.assert_not_awaited()


async def test_failed_creation_changes_notice_to_retry_guidance() -> None:
    message, edit = _message()

    async def opening() -> discord.Thread:
        await asyncio.sleep(0.02)
        raise RuntimeError("thread creation failed")

    with pytest.raises(RuntimeError, match="thread creation failed"):
        await _open_thread_with_notice(message, opening(), guild_id="456", after_s=0)
    edit.assert_awaited_once_with(
        content="I couldn't open your chat. Please try mentioning me again."
    )
