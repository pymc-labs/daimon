"""A routine on a fresh install fires from the scheduler's tick and records its answer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx
from daimon.adapters.mcp.server import create_mcp_app  # pyright: ignore[reportUnknownVariableType]
from daimon.adapters.scheduler.main import _build_fire  # pyright: ignore[reportPrivateUsage]
from daimon.core.authz import Subject, build_subject
from daimon.core.routine_service import (
    NewRoutine,
    Placement,
    RoutineCaller,
    RoutineEnv,
    create_routine,
)
from daimon.core.scheduler import run_one_tick
from daimon.core.stores import agent_tool_rules
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import get_routine
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolSafetyPolicy
from daimon.testing.asgi import asgi_lifespan
from daimon.testing.sim.discord import DiscordAPI
from daimon.testing.sim.script import Tool, Turn
from mux.drivers.gemini.core import BUILTIN_TOOLS
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .discord import CHANNEL, GUILD, PERSON, Discord
from .test_without_anthropic import MCP, _Daimon  # pyright: ignore[reportPrivateUsage]
from .world import World, metering, world


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


@asynccontextmanager
async def _gemini_install(
    engine: AsyncEngine, **overrides: Any
) -> AsyncIterator[tuple[World, Discord]]:
    """A Gemini-only install with Daimon's own MCP server reachable, as the provider sees it."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with world(sessions, keyed=("gemini",), mcp=MCP, **overrides) as w:
        rest = DiscordAPI(guild=GUILD, owner=PERSON)
        w.network.fakes[rest.host] = rest
        app = create_mcp_app(settings=w.settings, sessionmaker=w.sessions, anthropic=w.anthropic)
        daimon = _Daimon(httpx.ASGITransport(app=app))  # pyright: ignore[reportArgumentType]
        w.network.fakes[daimon.host] = daimon
        async with asgi_lifespan(app):
            discord = Discord(w)
            await discord.install()
            yield w, discord


async def _scheduled(w: World, discord: Discord, **destination: Any) -> RoutineRow:
    """The admin's daily routine for the default agent."""
    caller = RoutineCaller(
        tenant_id=discord.tenant_id,
        subject=build_subject(is_admin=True, platform_user_id=str(PERSON)),
        platform="discord",
        is_admin=True,
    )
    new = NewRoutine(
        agent_name=w.deps.deployment_default.agent_name or "",
        cron_expr="0 9 * * *",
        timezone="UTC",
        trigger_message="find the launch notes",
        **destination,
    )

    async def placed(kind: str, destination_id: str) -> Placement:
        # A thread posts under the one channel this install has.
        return Placement(destination_id, str(CHANNEL) if kind == "thread" else None)

    env = RoutineEnv(w.sessions, w.anthropic, resolve_destination=placed)
    routine = await create_routine(env, caller, new)
    assert routine.next_fire_at is not None, "a new routine is scheduled"
    return routine


async def _fire(w: World, routine: RoutineRow) -> RoutineRow:
    """Fire `routine` from the scheduler's tick and read it back."""
    assert routine.next_fire_at is not None
    fire = await _build_fire(
        client=w.anthropic,
        sm=w.sessions,
        settings=w.settings,
        deployment_default=w.deps.deployment_default,
        resolver_cache=w.deps.resolver_cache,
        dm_turn_deps=w.deps,
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
        fired = await get_routine(db, routine.id, tenant_id=routine.tenant_id)
    assert fired is not None
    return fired


async def test_a_gemini_routine_runs_on_gemini_without_built_in_tools(
    db_preflight: None, db_nullpool_engine: AsyncEngine
) -> None:
    """A Gemini-only install's routine runs on Gemini through the chat path, metered, with
    no Anthropic request. Without an operator's sandbox opt-in its session leaves the
    seeded agent's built-in tools out, as Managed Agents does."""
    async with _gemini_install(db_nullpool_engine) as (w, discord):
        routine = await _scheduled(w, discord)
        w.script.add(Turn("Found the launch notes.", input_tokens=1500, output_tokens=90))
        fired = await _fire(w, routine)
        assert fired.last_error is None, f"it ran: {fired}"
        assert "Found the launch notes." in (fired.last_result_tail or ""), fired.last_result_tail
        sent = [t for i in w.gemini.interactions.values() for t in i.raw.get("tools") or []]
        assert sent and not [t for t in sent if t.get("type") in BUILTIN_TOOLS], sent
        metered, billed = await metering(w, discord.tenant_id, "gemini")
        assert metered == billed and w.gemini.reported, f"metered on Gemini: {metered} != {billed}"
        assert not w.api.seen, f"no Anthropic request on a keyless install: {w.api.seen}"
        w.network.assert_clean()


async def test_a_gemini_routine_follows_its_channels_tool_rules_unattended(
    db_preflight: None, db_nullpool_engine: AsyncEngine
) -> None:
    """A rule in the routine's channel reaches the call on Gemini, which the MCP server
    decides by the fire's unattended origin, in the words a Claude routine gets."""
    safety = ToolSafetyPolicy(enabled=True, agent_rules=True)
    async with _gemini_install(db_nullpool_engine, tool_safety=safety) as (w, discord):
        routine = await _scheduled(
            w, discord, destination_kind="channel", destination_id=str(CHANNEL)
        )
        async with w.sessions.begin() as db:
            await agent_tool_rules.set_rule(
                db,
                tenant_id=discord.tenant_id,
                agent_id=routine.agent_id,
                agent_name=routine.agent_name,
                tool_key=f"{DAIMON_SERVER_NAME}/list_agents",
                channel_id=str(CHANNEL),
                server_url=str(MCP.public_url),
                rule="deny",
                set_by="admin",
                actor=Subject(is_admin=True),
            )
        listed = Tool("list_agents", server="daimon_mcp", live=True)
        w.script.add(Turn("Listed.", tool=listed))
        fired = await _fire(w, routine)
    assert fired.last_error is None, f"it ran: {fired}"
    results = [
        step["result"]
        for interaction in w.gemini.interactions.values()
        for step in interaction.raw["steps"]
        if step["type"] == "mcp_server_tool_result"
    ]
    assert results == [
        f"Refused: this agent's tool rule denies {DAIMON_SERVER_NAME}/list_agents. Do not retry it."
    ], "the channel's deny decides the call, as on Claude"
