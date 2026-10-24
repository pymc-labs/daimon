"""The `help` command lists the registered commands and how to reach the bot."""

from __future__ import annotations

import json
import uuid
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.help import COMMAND_HELP, help_card, send_help
from daimon.adapters.teams.private_replies import IN_THREAD
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.platform_words import DESCRIPTIONS, WORDS
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    TeamsApiFake,
    build_teams_runtime,
    make_inbound,
    make_message_activity,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")


ALL = [*COMMAND_HELP]
ALL_WITHOUT_SUPPORT = [name for name in ALL if name != "support"]


def _card(names: list[str], *, place: Any = "dm") -> dict[str, Any]:
    return help_card(names, bot="daimon", place=place).model_dump(by_alias=True)


def _texts(card: dict[str, Any]) -> list[str]:
    return [item["text"] for item in card["body"] if item["type"] == "TextBlock"]


def _listed(card: dict[str, Any]) -> list[str]:
    facts = next(item["facts"] for item in card["body"] if item["type"] == "FactSet")
    return [fact["title"] for fact in facts]


def test_command_descriptions_come_from_the_shared_table() -> None:
    for action, command in WORDS["teams"].commands.items():
        assert COMMAND_HELP[command.word] == DESCRIPTIONS[action]
    assert COMMAND_HELP["here"] == "Who answers here and what it can use"


def test_help_card_lists_only_registered_commands() -> None:
    card = _card(["new", "help"])

    assert _listed(card) == ["new", "help"], "registered commands are listed"


def test_one_to_one_card_lists_every_registered_command_and_says_just_send_a_word() -> None:
    card = _card(ALL)

    assert _listed(card) == ALL
    assert _texts(card) == [
        "📖 Commands",
        "Send one of these words.",
        "💬 Or just ask",
        "Help me set up",
    ]


def test_channel_card_lists_only_what_is_answered_there_and_points_to_the_chat() -> None:
    card = _card(ALL, place="channel")

    assert set(_listed(card)) == IN_THREAD
    assert _texts(card) == [
        "📖 Commands",
        "Only you can see this. @mention daimon with one of these words.",
        "In our 1:1 chat: setup, support",
        "To start over, start a new post.",
        "💬 Or just ask",
        "@mention daimon with your question.",
    ]


def test_channel_card_names_support_only_when_it_is_registered() -> None:
    assert "In our 1:1 chat: setup" in _texts(_card(ALL_WITHOUT_SUPPORT, place="channel"))


def test_group_chat_card_also_lists_new_and_has_no_new_post_line() -> None:
    card = _card(ALL, place="group")

    assert set(_listed(card)) == IN_THREAD | {"new"}
    assert "In our 1:1 chat: setup, support" in _texts(card)
    assert not any("new post" in text for text in _texts(card))


@pytest.mark.parametrize("place", ["dm", "channel", "group"])
def test_no_card_claims_a_slash_menu_or_another_platforms_command(place: Any) -> None:
    text = json.dumps(_card(ALL, place=place))

    assert "type /" not in text
    for foreign in ("/agent-setup", "/github", "/dm-me", "/billing", "server admin"):
        assert foreign not in text


@pytest.mark.parametrize(
    ("kind", "asked_kind", "expected"),
    [
        ("dm", None, "Send one of these words."),
        ("channel", None, "Only you can see this. @mention daimon with one of these words."),
        ("group", None, "Only you can see this. @mention daimon with one of these words."),
        ("dm", "channel", "Only you can see this. @mention daimon with one of these words."),
    ],
)
async def test_send_help_picks_the_card_for_where_help_was_sent(
    kind: Any, asked_kind: Any, expected: str
) -> None:
    send = AsyncMock()
    context = CommandContext(
        inbound=make_inbound(kind=kind),
        tenant_id=uuid.uuid4(),
        args="",
        is_admin=False,
        runtime=cast(TeamsRuntime, None),
        send=send,
        asked_in=make_inbound(kind=asked_kind) if asked_kind else None,
    )
    await send_help(context, names=ALL)

    send.assert_awaited_once()
    [attachment] = send.call_args.args[0].attachments
    assert _texts(attachment.content.model_dump(by_alias=True))[1] == expected


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
    assert set(listed) == set(COMMAND_HELP) - {"support"}, (
        "every wired command is listed; support only when escalation is configured"
    )
