"""Which channels a person may point a routine at: the chat tool's rule, shared with the web app."""

from __future__ import annotations

import pytest
from daimon.core.channel_visibility import (
    DISCORD_ADMINISTRATOR,
    DISCORD_SEND_MESSAGES,
    DISCORD_VIEW_CHANNEL,
    DiscordMember,
    DiscordOverwrite,
)
from daimon.core.routine_destinations import discord_deliverable_channels, slack_may_deliver

GUILD = "g1"
VIEW = DISCORD_VIEW_CHANNEL
SEND = DISCORD_SEND_MESSAGES
ME = DiscordMember(user_id="u1", role_ids=frozenset({"staff"}))


def _deliverable(member: DiscordMember, roles: dict[str, int]) -> frozenset[str]:
    overwrites = {
        "open": (),
        "read-only": (DiscordOverwrite(GUILD, 0, allow=0, deny=SEND),),
        "staff-can-send": (
            DiscordOverwrite(GUILD, 0, allow=0, deny=SEND),
            DiscordOverwrite("staff", 0, allow=SEND, deny=0),
        ),
        "hidden": (DiscordOverwrite(GUILD, 0, allow=0, deny=VIEW | SEND),),
        "muted-you": (DiscordOverwrite(member.user_id, 1, allow=0, deny=SEND),),
    }
    return discord_deliverable_channels(
        member,
        guild_id=GUILD,
        role_permissions=roles,
        channel_overwrites=overwrites,
        channel_ids=[*overwrites, "gone"],
    )


def test_a_member_delivers_where_they_can_view_and_send() -> None:
    found = _deliverable(ME, {GUILD: VIEW | SEND})
    assert found == {"open", "staff-can-send"}, "read-only, hidden and muted channels are refused"


def test_viewing_alone_is_not_enough() -> None:
    assert _deliverable(ME, {GUILD: VIEW}) == {"staff-can-send"}, (
        "only the role overwrite grants send"
    )


def test_a_role_that_grants_send_applies() -> None:
    assert "open" in _deliverable(ME, {GUILD: VIEW, "staff": SEND})


def test_an_administrator_delivers_everywhere_the_guild_has() -> None:
    found = _deliverable(ME, {GUILD: 0, "staff": DISCORD_ADMINISTRATOR})
    assert "gone" not in found
    assert found == {"open", "read-only", "staff-can-send", "hidden", "muted-you"}


def test_the_owner_delivers_everywhere_the_guild_has() -> None:
    owner = DiscordMember(user_id="u1", role_ids=frozenset(), is_owner=True)
    assert "read-only" in _deliverable(owner, {GUILD: 0})


@pytest.mark.parametrize(
    ("facts", "allowed"),
    [
        ({}, True),
        ({"bot_is_member": False}, False),
        ({"is_archived": True}, False),
        ({"is_im_or_mpim": True}, False),
        ({"is_private": True, "caller_is_member": False}, False),
        ({"is_private": True, "caller_is_member": True}, True),
        ({"is_guest": True, "caller_is_member": False}, False),
    ],
)
def test_slack_needs_daimon_in_the_channel_and_the_callers_access(
    facts: dict[str, bool], allowed: bool
) -> None:
    base = {
        "bot_is_member": True,
        "is_archived": False,
        "is_im_or_mpim": False,
        "is_private": False,
        "is_guest": False,
        "caller_is_member": True,
    }
    assert slack_may_deliver(**(base | facts)) is allowed


def test_a_timed_out_member_delivers_nowhere_but_an_administrator_still_does() -> None:
    timed_out = DiscordMember(user_id="u1", role_ids=frozenset(), timed_out=True)
    assert _deliverable(timed_out, {GUILD: VIEW | SEND}) == frozenset()
    admin = DiscordMember(user_id="u1", role_ids=frozenset({"staff"}), timed_out=True)
    assert "open" in _deliverable(admin, {GUILD: 0, "staff": DISCORD_ADMINISTRATOR})
