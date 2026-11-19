"""An install keyed only for OpenAI or Gemini: the seeded default agent replies, through the real bot."""

from __future__ import annotations

import httpx
import pytest
from daimon.adapters.mcp.server import create_mcp_app  # pyright: ignore[reportUnknownVariableType]
from daimon.core.config import McpSettings
from daimon.testing.asgi import asgi_lifespan
from daimon.testing.sim.discord import DiscordAPI
from daimon.testing.sim.network import StrictFake
from daimon.testing.sim.script import Turn
from mux.contracts.ids import Provider
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .discord import GUILD, PERSON, Discord
from .world import enable_backend, world

MCP = McpSettings(
    public_url=HttpUrl("https://daimon.example.com/mcp"),
    jwt_secret=SecretStr("sim-mcp-secret-of-at-least-32-bytes"),
)


class _Daimon(StrictFake):
    """Daimon's own MCP server at its public URL, as the provider and the catalogue read reach it."""

    def __init__(self, transport: httpx.ASGITransport) -> None:
        super().__init__(host="daimon.example.com")
        for method in ("GET", "POST", "DELETE"):
            self.route(method, "/mcp", lambda r, m: transport.handle_async_request(r))

    def refuse(self, status: int, message: str, param: str | None = None) -> httpx.Response:
        return httpx.Response(status, json={"error": message})


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_the_seeded_agent_replies_on_a_keyless_install(
    provider: Provider, db_preflight: None, db_nullpool_engine: AsyncEngine
) -> None:
    """The seeded agent is born on the keyed provider, with no Claude source to switch from."""
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with world(sessions, keyed=(provider,), mcp=MCP) as w:
        app = create_mcp_app(settings=w.settings, sessionmaker=w.sessions, anthropic=w.anthropic)
        rest = DiscordAPI(guild=GUILD, owner=PERSON)
        w.network.fakes[rest.host] = rest
        daimon = _Daimon(httpx.ASGITransport(app=app))  # pyright: ignore[reportArgumentType]
        w.network.fakes[daimon.host] = daimon
        async with asgi_lifespan(app):
            d = Discord(w)
            await d.install()
            # A key makes the provider available; sending conversations there is the admin's opt-in.
            await enable_backend(w, d.tenant_id, provider)
            w.script.add(Turn("Hello."))
            w.script.add(Turn("Still here."))
            first = await d.say("hello")
            assert "Hello." in first[-1], f"the seeded agent replies: {first}"
            thread = d.thread_of_last_reply()
            again = await d.say("are you there?", thread=thread)
            assert "Still here." in again[-1], f"the next turn replies too: {again}"
        fake = w.openai if provider == "openai" else w.gemini
        assert len(fake.reported) == 3, (
            f"the title and both turns ran on {provider}: {fake.reported}"
        )
        assert not w.api.seen, f"no Anthropic request on a keyless install: {w.api.seen}"
        assert not d.bot.errors, f"the bot raised: {d.bot.errors}"
        w.network.assert_clean()
