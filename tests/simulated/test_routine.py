"""A routine on a fresh install fires from the scheduler's tick and records its answer."""

from __future__ import annotations

from datetime import timedelta

from daimon.adapters.scheduler.main import _build_fire  # pyright: ignore[reportPrivateUsage]
from daimon.core.authz import build_subject
from daimon.core.routine_service import NewRoutine, RoutineCaller, RoutineEnv, create_routine
from daimon.core.scheduler import run_one_tick
from daimon.core.stores.routines import get_routine
from daimon.testing.sim.discord import DiscordAPI
from daimon.testing.sim.script import Tool, Turn
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .discord import GUILD, PERSON, Discord
from .world import metering, world


async def test_a_routine_fires_and_records_its_answer(
    db_preflight: None, db_nullpool_engine: AsyncEngine
) -> None:
    """An admin schedules the default agent; the tick runs it headless and meters it."""
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with world(sessions) as w:
        rest = DiscordAPI(guild=GUILD, owner=PERSON)  # the scheduler checks membership over REST
        w.network.fakes[rest.host] = rest
        discord = Discord(w)
        await discord.install()
        tenant_id = discord.tenant_id
        caller = RoutineCaller(
            tenant_id=tenant_id,
            subject=build_subject(is_admin=True, platform_user_id=str(PERSON)),
            platform="discord",
            is_admin=True,
        )
        new = NewRoutine(
            agent_name=w.deps.deployment_default.agent_name or "",
            cron_expr="0 9 * * *",
            timezone="UTC",
            trigger_message="find the launch notes",
        )
        routine = await create_routine(RoutineEnv(w.sessions, w.anthropic), caller, new)
        assert routine.next_fire_at is not None, "a new routine is scheduled"
        tool = Tool("discord_search_messages", {"query": "launch notes"}, server="daimon-mcp")
        w.script.add(Turn("Found the launch notes.", tool, input_tokens=1500, output_tokens=90))
        fire = await _build_fire(
            client=w.anthropic,
            sm=w.sessions,
            settings=w.settings,
            deployment_default=w.deps.deployment_default,
            resolver_cache=w.deps.resolver_cache,
        )
        await run_one_tick(
            now=routine.next_fire_at + timedelta(seconds=1),
            sm=w.sessions,
            fire=fire,
            max_age=timedelta(minutes=15),
            max_concurrent_fires=1,
            dispatch_timeout_s=60,
            wait_for_completion=True,
        )
        async with w.sessions() as db:
            fired = await get_routine(db, routine.id, tenant_id=tenant_id)
        assert fired is not None and fired.last_error is None, f"it ran: {fired}"
        assert "Found the launch notes." in (fired.last_result_tail or ""), fired.last_result_tail
        metered, billed = await metering(w, tenant_id, "anthropic")
        assert metered == billed, f"the routine's turn is metered: {metered} != {billed}"
        w.network.assert_clean()
