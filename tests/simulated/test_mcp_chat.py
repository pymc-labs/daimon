"""An MCP client asks a fresh install's agent, as the plugin does with a coding key."""

from __future__ import annotations

import datetime as dt
import json

from daimon.adapters.mcp.server import create_mcp_app  # pyright: ignore[reportUnknownVariableType]
from daimon.core.agent_editing import AgentActor, AgentEditDeps
from daimon.core.authz import Subject, Surface
from daimon.core.coding_keys import mint_coding_key
from daimon.core.config import McpSettings
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.sim.discord import DiscordAPI
from daimon.testing.sim.script import Tool, Turn
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .discord import GUILD, PERSON, Discord
from .world import metering, world

SECRET = "sim-mcp-secret-of-at-least-32-bytes"


async def test_ask_answers_through_the_agent_and_meters_it(
    db_preflight: None, db_nullpool_engine: AsyncEngine
) -> None:
    """A coding key reaches the seeded agent; `ask` waits for the reply; usage is metered."""
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    mcp = McpSettings(
        public_url=HttpUrl("https://daimon.example.com/mcp"), jwt_secret=SecretStr(SECRET)
    )
    async with world(sessions, mcp=mcp) as w:
        rest = DiscordAPI(guild=GUILD, owner=PERSON)  # the MCP host checks membership over REST
        w.network.fakes[rest.host] = rest
        discord = Discord(w)
        await discord.install()
        tenant_id = discord.tenant_id
        (agent,) = [a for a in w.api.ma.agents.values() if a["name"] == "default"]
        async with w.sessions.begin() as db:  # an admin, as the GitHub connect flow makes one
            person = await get_or_create_platform_principal(
                db, tenant_id=tenant_id, platform="discord", external_id=str(PERSON)
            )
            await set_role(db, person.account_id, Role.ADMIN)
        actor = AgentActor(
            tenant_id=tenant_id,
            account_id=person.account_id,
            subject=Subject(is_admin=True, platform_user_id=str(person.account_id)),
            surface=Surface.WEB,
        )
        edit = AgentEditDeps(w.anthropic, w.sessions, public_url=str(mcp.public_url))
        key = await mint_coding_key(  # the web app's "Create coding key"
            edit,
            actor=actor,
            ma_agent_id=str(agent["id"]),
            public_url=str(mcp.public_url),
            secret=SECRET.encode(),
            now=dt.datetime.now(dt.UTC),
        )
        tool = Tool("discord_search_messages", {"query": "launch notes"}, server="daimon-mcp")
        w.script.add(Turn("Found the launch notes.", tool, input_tokens=1500, output_tokens=90))
        app = create_mcp_app(settings=w.settings, sessionmaker=w.sessions, anthropic=w.anthropic)
        answer = await call_mcp_tool(
            app, token=key.token, name="ask", arguments={"message": "find the launch notes"}
        )
        result = json.dumps(answer.get("result"))
        assert "Found the launch notes." in result, f"ask returns the reply: {answer}"
        metered, billed = await metering(w, tenant_id, "anthropic")
        assert metered == billed, f"the MCP turn is metered: {metered} != {billed}"
        w.network.assert_clean()
