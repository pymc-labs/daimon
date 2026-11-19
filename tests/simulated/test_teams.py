"""A Teams turn from a fresh install: boot provisioning, then the real Bot Framework service."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.fernet import Fernet
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.http_service import create_teams_http_service
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.config import CryptoSettings, TeamsSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.testing.asgi import asgi_lifespan
from daimon.testing.sim.script import Tool, Turn
from daimon.testing.sim.teams import BotFrameworkAPI
from microsoft_teams.apps.token_manager import TokenManager
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .world import metering, switch_to, world

ENTRA, PERSON = str(uuid.UUID(int=0x5E7)), str(uuid.UUID(int=0xB0B))
CHANNEL, BOT = "19:sim@thread.tacv2", "28:sim-bot"


def _mention(text: str) -> dict[str, Any]:
    """A channel post that @mentions the bot, as Bot Framework delivers it."""
    mention = "<at>daimon</at>"
    return {
        "type": "message",
        "id": "1700000000100",
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/teams",
        "from": {"id": f"29:{PERSON}", "aadObjectId": PERSON},
        "recipient": {"id": BOT, "name": "daimon"},
        "conversation": {
            "id": CHANNEL,
            "conversationType": "channel",
            "tenantId": ENTRA,
            "isGroup": True,
        },
        "channelData": {
            "tenant": {"id": ENTRA},
            "channel": {"id": CHANNEL},
            "team": {"id": "19:team@thread.tacv2"},
        },
        "text": f"{mention} {text}",
        "entities": [{"type": "mention", "text": mention, "mentioned": {"id": BOT}}],
    }


async def _idle(app: TeamsApp) -> None:
    """Wait for the work the service spawned: its boot sweep, provisioning and turns."""
    async with asyncio.timeout(30):
        await app._recovery  # pyright: ignore[reportPrivateUsage, reportGeneralTypeIssues]
        while tasks := list(app._tasks):  # pyright: ignore[reportPrivateUsage]
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_a_fresh_install_answers_on_gemini(
    db_preflight: None, db_nullpool_engine: AsyncEngine
) -> None:
    """The service provisions its tenant at boot; an admin picks Gemini; a mention is answered."""
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    teams = TeamsSettings(
        client_id="sim-bot", client_secret=SecretStr("sim"), tenant_id=ENTRA, enabled=True
    )
    crypto = CryptoSettings(keys=(SecretStr(Fernet.generate_key().decode()),))
    async with world(sessions, teams=teams, crypto=crypto) as w, httpx.AsyncClient() as http:
        bot = BotFrameworkAPI(tenant=ENTRA)
        w.network.fakes[bot.host] = bot
        runtime = TeamsRuntime(
            settings=w.settings,
            anthropic=w.anthropic,
            sessionmaker=w.sessions,
            billing_config=None,
            http_client=http,
            resolver_cache=w.deps.resolver_cache,
            turn_deps=w.deps,
            deployment_default=w.deps.deployment_default,
        )
        tenant_id = derive_tenant_uuid(platform="teams", workspace_id=ENTRA)
        tool = Tool("discord_search_messages", {"query": "launch notes"}, server="daimon-mcp")
        w.script.add(Turn("Found the launch notes.", tool, input_tokens=1500, output_tokens=90))
        # Bot Framework's sign-in (MSAL) and its JWT checks are the platform's, not ours.
        with (
            patch.dict(os.environ, {"DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS": "true"}),
            patch.object(TokenManager, "get_app_token", AsyncMock(return_value="sim-token")),
        ):
            service = create_teams_http_service(settings=teams, runtime=runtime)
            async with asgi_lifespan(service.app):
                await _idle(service.turns)  # the boot sweep provisioned and seeded the tenant
                await switch_to(w, tenant_id, "gemini", platform="teams", user_id=PERSON)
                transport = httpx.ASGITransport(app=service.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://sim") as ingress:
                    response = await ingress.post("/api/messages", json=_mention("launch notes?"))
                await _idle(service.turns)
                assert response.status_code == 200, f"{response.status_code}: {response.text}"
        shown, ids = bot.texts(), [i for i, _ in bot.activities]
        assert shown[0].startswith("**Working on it"), f"the turn shows progress first: {shown}"
        assert ids[1] == ids[0] and shown[1] == "Found the launch notes.", f"reply: {shown}"
        assert "used" in shown[-1] and "gemini" in shown[-1], f"the footer shows cost: {shown}"
        metered, billed = await metering(w, tenant_id, "gemini")
        assert metered == billed and w.gemini.reported, f"metered on Gemini: {metered} != {billed}"
        w.network.assert_clean()
