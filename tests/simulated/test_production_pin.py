"""A Discord turn with the backend switches off, as production pins them."""

from __future__ import annotations

from daimon.core.config import TurnSettings
from daimon.testing.sim.script import Tool, Turn
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .discord import Discord
from .world import metering, world


async def test_a_turn_with_channel_and_agent_backends_off(
    db_preflight: None, db_nullpool_engine: AsyncEngine
) -> None:
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    pinned = TurnSettings(channel_backends=False, agent_backends=False)
    async with world(sessions, turn=pinned) as w:
        d = Discord(w)
        await d.install()
        tool = Tool("discord_search_messages", {"query": "launch notes"}, server="daimon-mcp")
        w.script.add(Turn("Found the launch notes.", tool, input_tokens=1500, output_tokens=90))

        shown = await d.say("find the launch notes")
        assert shown[0].startswith("\nWorking on it"), f"the turn shows progress first: {shown}"
        assert "Found the launch notes." in shown[-1], f"the reply is posted: {shown}"
        metered, billed = await metering(w, d.tenant_id, "anthropic")
        assert metered == billed, f"a turn is metered as it ends: {metered} != {billed}"
        w.network.assert_clean()
