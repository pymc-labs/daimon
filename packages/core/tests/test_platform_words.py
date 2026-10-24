from __future__ import annotations

import pytest
from daimon.core.platform_words import DESCRIPTIONS, WORDS, turn_words, words_for


def test_teams_setup_points_to_the_private_chat_outside_it() -> None:
    teams = words_for("teams")
    assert teams.say("setup", place="channel") == "send `setup` in a 1:1 chat with me"
    assert teams.say("setup", place="dm") == "send `setup`"
    assert teams.say("here", place="channel") == "send `here`"


def test_discord_and_slack_use_slash_commands() -> None:
    for platform in ("discord", "slack"):
        assert words_for(platform).say("setup", place="channel") == "run `/agent-setup`"


def test_teams_has_no_github_or_dm_me_command() -> None:
    teams = words_for("teams")
    assert teams.say("github", place="dm") is None
    assert teams.say("dm_me", place="channel") is None


@pytest.mark.parametrize(
    ("platform", "place", "expected"),
    [
        ("teams", "channel", "start a new post"),
        ("teams", "group", "send `new`"),
        ("teams", "dm", "send `new`"),
        ("discord", "channel", "start a new thread"),
        ("slack", "dm", "ask me to start fresh"),
    ],
)
def test_fresh_start(platform: str, place: str, expected: str) -> None:
    assert WORDS[platform].fresh_start(place=place) == expected  # type: ignore[index, arg-type]


def test_top_up() -> None:
    assert words_for("teams").top_up() == "`billing` in a 1:1 chat with me"
    assert words_for("slack").top_up() == "`/billing`"


def test_turn_words_name_no_foreign_command() -> None:
    teams = turn_words("teams", place="channel")
    assert teams["setup"] == "send `setup` in a 1:1 chat with Daimon"
    assert teams["admin"].startswith("an admin (")
    assert not any("/" in value for value in teams.values())
    discord = turn_words("discord", place="channel")
    assert discord["setup"] == "run `/agent-setup`"
    assert "Manage Server" in discord["admin"]


def test_every_command_has_a_description() -> None:
    for words in WORDS.values():
        assert set(words.commands) <= set(DESCRIPTIONS)


def test_discord_server_commands_name_a_server_channel_from_a_dm() -> None:
    discord = words_for("discord")
    assert discord.say("setup", place="dm") == "run `/agent-setup` in a server channel"
    assert discord.top_up(place="dm") == "`/billing` in a server channel"
    assert discord.say("privacy", place="dm") == "run `/privacy`"


def test_slack_commands_work_in_a_dm_except_dm_me() -> None:
    slack = words_for("slack")
    assert slack.say("setup", place="dm") == "run `/agent-setup`"
    assert slack.say("dm_me", place="dm") == "run `/dm-me` in a workspace channel"


def test_teams_new_works_in_a_group_chat_and_support_needs_the_private_chat() -> None:
    teams = words_for("teams")
    assert teams.say("new", place="group") == "send `new`"
    assert teams.say("support", place="channel") == "send `support` in a 1:1 chat with me"
