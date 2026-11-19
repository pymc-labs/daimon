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


def _fake(w: World, provider: Provider):
    return {"anthropic": w.api, "openai": w.openai, "gemini": w.gemini}[provider]


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_a_turn_streams_calls_a_tool_and_survives_a_restart(
    provider: Provider, db_preflight: None, db_nullpool_engine: AsyncEngine
) -> None:
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with world(sessions) as w:
        d = Discord(w)
        await d.install()
        await _on(w, d, provider)
        w.script.add(Turn("Found the launch notes.", SEARCH, input_tokens=1500, output_tokens=90))
        w.script.add(Turn("Still here after the restart.", input_tokens=1700, output_tokens=40))

        first = await d.say("find the launch notes")
        assert first[0].startswith("\nWorking on it"), f"the turn shows progress first: {first}"
        assert "Found the launch notes." in first[-1], f"the reply is posted: {first}"
        thread = d.thread_of_last_reply()

        await d.restart()
        second = await d.say("are you still there?", thread=thread)
        assert "Still here after the restart." in second[-1], f"the restarted bot replies: {second}"

        async with sessions() as session:
            rows = await usage_events.list_for_tenant(session, tenant_id=d.tenant_id)
        fake = _fake(w, provider)
        # Thread titles are Daimon's own Claude call, whichever provider answers.
        billed = w.api.reported + ([] if fake is w.api else fake.reported)
        metered = sorted((r.input_tokens, r.output_tokens) for r in rows)
        assert metered == sorted(billed), f"one usage row per model call: {metered}"
        assert not d.bot.errors, f"the bot raised: {d.bot.errors}"
        w.network.assert_clean()
