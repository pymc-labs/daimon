"""Tests for the MCP app's shared Slack aiohttp session."""

from __future__ import annotations

from daimon.adapters.mcp.slack_http import SlackHttpSession


async def test_get_reuses_one_open_session_until_close() -> None:
    """Every Slack call between startup and shutdown rides the same pool."""
    holder = SlackHttpSession()
    first = holder.get()
    assert first is not None, "a running app gets a session"
    assert holder.get() is first, "a second call must reuse the open session"

    await holder.close()

    assert first.closed, "close releases the session's connections"
    assert holder.get() is None, (
        "a call after shutdown falls back to slack_sdk's per-call session, "
        "not a new shared one nothing would close"
    )


async def test_close_without_get_is_a_no_op() -> None:
    """An app that never called Slack shuts down cleanly."""
    holder = SlackHttpSession()
    await holder.close()
    assert holder.get() is None, "nothing is opened after shutdown"
