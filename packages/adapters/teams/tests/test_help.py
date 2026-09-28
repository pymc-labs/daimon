"""The `help` command lists the registered commands and how to reach the bot."""

from __future__ import annotations

import json

import pytest
from daimon.adapters.teams.help import COMMAND_HELP, help_card
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    TeamsApiFake,
    build_teams_runtime,
    make_message_activity,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")


def test_help_card_lists_only_registered_commands() -> None:
    card = json.dumps(help_card(["new", "help"], bot="daimon").model_dump(by_alias=True))

    assert '"new"' in card and '"help"' in card, "registered commands are listed"
    assert '"billing"' not in card, "an unregistered command is not advertised"
    assert "@mention daimon" in card, "channels need an @mention"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_help_command_replies_with_every_registered_command(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    runtime = build_teams_runtime(db_session_factory)
    async with running_service(runtime, teams_api_fake) as service:
        await post_activity(service, make_message_activity(text="help"))
        await service.turns.drain(timeout=30)

    card = json.dumps(teams_api_fake.activity_requests[-1].body)
    listed = [name for name in COMMAND_HELP if f'"title": "{name}"' in card]
    assert set(listed) == set(COMMAND_HELP), "every wired command is listed"
