"""Discord turns on every provider, from a fresh install, through the real bot."""

from __future__ import annotations

import pytest
from daimon.core.stores import usage_events
from daimon.testing.sim.script import Tool, Turn
from mux.contracts.ids import Provider
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .discord import PERSON, Discord
from .world import World, enable_backend, set_agent_backend, world

SEARCH = Tool("discord_search_messages", {"query": "launch notes"}, server="daimon-mcp")


async def _on(w: World, d: Discord, provider: Provider) -> None:
    if provider != "anthropic":
        await enable_backend(w, d.tenant_id, provider)
        await set_agent_backend(
            w, d.tenant_id, platform="discord", user_id=str(PERSON), backend=provider
        )


async def _metering(
    w: World, d: Discord, provider: Provider
) -> tuple[list[tuple[int | None, int | None]], list[tuple[int, int]]]:
    """The usage rows Daimon wrote, and the calls the providers billed."""
    async with w.sessions() as session:
        rows = await usage_events.list_for_tenant(session, tenant_id=d.tenant_id)
    fake = {"anthropic": w.api, "openai": w.openai, "gemini": w.gemini}[provider]
    # Thread titles are Daimon's own Claude call, whichever provider answers.
    billed = w.api.reported + ([] if fake is w.api else fake.reported)
    return sorted((r.input_tokens, r.output_tokens) for r in rows), sorted(billed)


@pytest.mark.parametrize(
    ("provider", "late_usage"),
    [
        ("anthropic", False),
        ("openai", False),
        # Only the terminal event has the usage; the turn resource fills it in later (#1769).
        pytest.param(
            "openai",
            True,
            id="openai-late-usage",
            marks=pytest.mark.xfail(
                strict=True,
                reason="the late re-read meets the event's usage again at the same revision, "
                "now final, and retries on UsageRevisionConflict until its backoff ends",
            ),
        ),
        ("gemini", False),
    ],
)
async def test_a_turn_streams_calls_a_tool_and_survives_a_restart(
    provider: Provider, late_usage: bool, db_preflight: None, db_nullpool_engine: AsyncEngine
) -> None:
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with world(sessions) as w:
        w.openai.late_usage = late_usage
        d = Discord(w)
        await d.install()
        await _on(w, d, provider)
        w.script.add(Turn("Found the launch notes.", SEARCH, input_tokens=1500, output_tokens=90))
        w.script.add(Turn("Still here after the restart.", input_tokens=1700, output_tokens=40))

        first = await d.say("find the launch notes")
        assert first[0].startswith("\nWorking on it"), f"the turn shows progress first: {first}"
        assert "Found the launch notes." in first[-1], f"the reply is posted: {first}"
        thread = d.thread_of_last_reply()
        metered, billed = await _metering(w, d, provider)
        assert metered == billed, f"a turn is metered as it ends: {metered} != {billed}"

        await d.restart()
        second = await d.say("are you still there?", thread=thread)
        assert "Still here after the restart." in second[-1], f"the restarted bot replies: {second}"

        metered, billed = await _metering(w, d, provider)
        assert metered == billed, f"one usage row per model call: {metered} != {billed}"
        assert not d.bot.errors, f"the bot raised: {d.bot.errors}"
        w.network.assert_clean()


async def test_a_confirmed_tool_waits_for_approve(
    db_preflight: None, db_nullpool_engine: AsyncEngine
) -> None:
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with world(sessions) as w:
        d = Discord(w)
        await d.install()
        watch = Tool(
            "create_watch", {"source": "https://feeds.example.com/news"}, server="daimon-mcp"
        )
        w.script.add(Turn("Watching the feed now.", watch))

        asked = await d.say("watch the news feed")
        assert asked[-1].endswith("Approve\nDeny\nDetails"), f"a confirmation card: {asked}"
        assert w.api.sessions.events, "the model is paused on the tool call"

        done = await d.press("Approve")
        assert "Watching the feed now." in done[-1], f"the reply after Approve: {done}"
        assert not d.bot.errors, f"the bot raised: {d.bot.errors}"
        w.network.assert_clean()
