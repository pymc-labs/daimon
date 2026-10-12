"""Tests for the MCP app's shared Slack aiohttp session."""

from __future__ import annotations

from daimon.adapters.mcp.slack_http import SlackHttpSession


async def test_get_reuses_one_open_session_until_close() -> None:
    """Every Slack call between startup and shutdown rides the same pool."""
    holder = SlackHttpSession()
    first = holder.get()
    assert holder.get() is first, "a second call must reuse the open session"

    await holder.close()

    assert first.closed, "close releases the session's connections"
    reopened = holder.get()
    assert reopened is not first and not reopened.closed, "a later call opens a fresh session"
    await holder.close()


async def test_close_without_get_is_a_no_op() -> None:
    """An app that never called Slack shuts down cleanly."""
    holder = SlackHttpSession()
    await holder.close()
    assert not holder.get().closed, "the holder still opens a session afterwards"
    await holder.close()
