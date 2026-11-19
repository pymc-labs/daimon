"""Can Daimon post in the request channel: the rule per platform, and the fix it names."""

from __future__ import annotations

import pytest
from daimon.core.channel_visibility import (
    DISCORD_ADMINISTRATOR,
    DISCORD_EMBED_LINKS,
    DISCORD_SEND_MESSAGES,
    DISCORD_VIEW_CHANNEL,
)
from daimon.core.support_channel_check import (
    READY,
    ChannelCheck,
    discord_check,
    slack_check,
    teams_check,
)

_ALL = DISCORD_VIEW_CHANNEL | DISCORD_SEND_MESSAGES | DISCORD_EMBED_LINKS


@pytest.mark.parametrize(
    ("bits", "detail"),
    [
        (_ALL, None),
        (DISCORD_ADMINISTRATOR, None),
        (DISCORD_VIEW_CHANNEL | DISCORD_EMBED_LINKS, "Give Daimon Send Messages in #ask."),
        (DISCORD_VIEW_CHANNEL | DISCORD_SEND_MESSAGES, "Give Daimon Embed Links in #ask."),
        (DISCORD_VIEW_CHANNEL, "Give Daimon Send Messages and Embed Links in #ask."),
        (0, "Give Daimon View Channel, Send Messages and Embed Links in #ask."),
    ],
)
def test_discord_needs_view_send_and_embed_and_names_what_is_missing(
    bits: int, detail: str | None
) -> None:
    check = discord_check(bits=bits, label="#ask")
    expected = READY if detail is None else ChannelCheck("blocked", detail)
    assert check == expected, f"bits {bits:#x}"


@pytest.mark.parametrize(
    ("facts", "detail"),
    [
        ({"found": True}, None),
        ({"found": True, "is_private": False}, None),
        ({"found": True, "is_private": True, "bot_is_member": True}, None),
        ({"found": True, "is_private": True}, "Invite Daimon to #ask."),
        ({"found": False}, "Invite Daimon to #ask."),
        ({"found": True, "is_archived": True}, "#ask is archived."),
    ],
)
def test_slack_public_is_joined_at_post_time_and_private_needs_an_invite(
    facts: dict[str, bool], detail: str | None
) -> None:
    check = slack_check(label="#ask", **facts)
    expected = READY if detail is None else ChannelCheck("blocked", detail)
    assert check == expected, str(facts)


@pytest.mark.parametrize(
    ("team", "url", "detail"),
    [
        ("19:t", "https://smba.example.com/", None),
        (None, "https://smba.example.com/", "Pick #ask again to record its team id."),
        ("19:t", None, "Pick #ask again to record its service URL."),
        (None, None, "Pick #ask again to record its team id and service URL."),
    ],
)
def test_teams_pick_needs_its_team_and_service_url(
    team: str | None, url: str | None, detail: str | None
) -> None:
    check = teams_check(label="#ask", team_id=team, service_url=url)
    expected = READY if detail is None else ChannelCheck("blocked", detail)
    assert check == expected, f"{team} {url}"
