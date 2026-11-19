"""A conversation's own model pick (`/model`) onto another provider, through the real bot."""

from __future__ import annotations

from daimon.testing.sim.script import Turn
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .discord import CHANNEL, PERSON, Discord
from .world import enable_backend, pick_model, world


async def test_a_thread_picks_openai_and_keeps_replying(
    db_preflight: None, db_nullpool_engine: AsyncEngine
) -> None:
    """The pick's revision is stored as the conversation's; each turn re-puts it as such.

    Gemini never re-puts an admitted revision, so only OpenAI runs here.
    """
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with world(sessions) as w:
        d = Discord(w)
        await d.install()
        await enable_backend(w, d.tenant_id, "openai")
        w.script.add(Turn("Hello from Claude."))
        first = await d.say("hello")
        assert "Hello from Claude." in first[-1], f"the thread starts on Claude: {first}"
        thread = d.thread_of_last_reply()

        picked = await pick_model(
            w,
            d.tenant_id,
            platform="discord",
            channel_id=str(CHANNEL),
            thread_id=str(thread),
            user_id=str(PERSON),
            value="openai:gpt-6-luna",
        )
        assert picked, "the pick answers"
        w.script.add(Turn("Picked and replying."))
        w.script.add(Turn("Still on the pick."))
        reply = await d.say("are you there?", thread=thread)
        assert "Picked and replying." in reply[-1], f"the turn after the pick replies: {reply}"
        again = await d.say("and now?", thread=thread)
        assert "Still on the pick." in again[-1], f"the next turn replies too: {again}"
        assert len(w.openai.reported) == 2, f"both turns ran on OpenAI: {w.openai.reported}"
        assert not d.bot.errors, f"the bot raised: {d.bot.errors}"
        w.network.assert_clean()
