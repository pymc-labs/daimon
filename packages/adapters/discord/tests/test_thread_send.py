"""Tests for archive-safe thread message sending."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import discord
import pytest
from daimon.adapters.discord.thread_send import safe_thread_send


def _make_http_exception(code: int, message: str = "error") -> discord.HTTPException:
    """Construct a discord.HTTPException with the given error code."""
    response = Mock(status=400, reason="Bad Request")
    exc = discord.HTTPException(response, {"code": code, "message": message})
    return exc


class TestSafeThreadSend:
    async def test_sends_successfully(self) -> None:
        thread = AsyncMock(spec=discord.Thread)
        expected_msg = Mock(spec=discord.Message)
        thread.send.return_value = expected_msg

        result = await safe_thread_send(thread, "hello")

        assert result is expected_msg, "should return the sent message"
        thread.send.assert_called_once_with("hello")

    async def test_unarchives_and_retries_on_50083(self) -> None:
        thread = AsyncMock(spec=discord.Thread)
        thread.id = 12345
        expected_msg = Mock(spec=discord.Message)
        thread.send.side_effect = [
            _make_http_exception(50083, "Thread is archived"),
            expected_msg,
        ]

        result = await safe_thread_send(thread, "hello")

        assert result is expected_msg, "should return the retry message"
        thread.edit.assert_called_once_with(archived=False)
        assert thread.send.call_count == 2, "should have sent twice (original + retry)"

    async def test_reraises_non_archive_http_exception(self) -> None:
        thread = AsyncMock(spec=discord.Thread)
        non_archive_exc = _make_http_exception(50001, "Missing Access")

        thread.send.side_effect = non_archive_exc

        with pytest.raises(discord.HTTPException) as exc_info:
            await safe_thread_send(thread, "hello")

        assert exc_info.value.code == 50001, "should re-raise non-archive exceptions"
        thread.edit.assert_not_called()
