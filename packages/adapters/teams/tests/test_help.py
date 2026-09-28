"""The `help` command lists the registered commands and how to reach the bot."""

from __future__ import annotations

import json

import httpx
import pytest
from daimon.adapters.teams.help import COMMAND_HELP, help_card
from daimon.adapters.teams.http_service import create_teams_http_service
from daimon.core.defaults.provisioning import provision_tenant
from daimon.testing.asgi import asgi_lifespan
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    ENTRA_TENANT_ID,
    TeamsApiFake,
    build_teams_client,
    build_teams_runtime,
    make_message_activity,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")


def test_help_card_lists_only_registered_commands() -> None:
    card = json.dumps(help_card(["new", "help"], bot="daimon").model_dump(by_alias=True))

    assert '"new"' in card and '"help"' in card, "registered commands are listed"
    assert '"billing"' not in card, "an unregistered command is not advertised"
    assert "@mention daimon" in card, "channels need an @mention"


@pytest.mark.asyncio
async def test_help_command_replies_with_every_registered_command(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    settings = teams_settings()
    runtime = build_teams_runtime(db_session_factory, teams=settings)
    service = create_teams_http_service(
        settings=settings, runtime=runtime, client=build_teams_client(teams_api_fake)
    )
    async with asgi_lifespan(service.app):
        await service.turns.start()
        transport = httpx.ASGITransport(app=service.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/messages", json=make_message_activity(text="help"))
        assert response.status_code == 200, response.text
        await service.turns.drain(timeout=30)

    card = json.dumps(teams_api_fake.activity_requests[-1].body)
    listed = [name for name in COMMAND_HELP if f'"title": "{name}"' in card]
    assert set(listed) == set(COMMAND_HELP), "every wired command is listed"
