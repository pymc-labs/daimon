"""The warning screens read the same per-person room check that the facts use."""

from __future__ import annotations

import discord
from daimon.adapters.discord.outsiders import channel_outsiders, guild_rooms, is_public_thread
from daimon.core.outsiders import INTERNAL_ROLES_HINT

from . import audience_fakes as fake

INTERNAL = (str(fake.CORE), str(fake.FRIEND))
TEAM = (str(fake.CORE),)


def _open(channel_id: int = 1) -> object:
    return fake.channel(channel_id, "planning", fake.internal_only())


def _guest(channel_id: int = 2, name: str = "guest") -> object:
    overwrites = fake.internal_only()
    overwrites[fake.target(discord.User, 30)] = discord.PermissionOverwrite(view_channel=True)
    return fake.channel(channel_id, name, overwrites)


def test_without_internal_roles_does_not_claim_visibility() -> None:
    result = channel_outsiders(_guest(), (), ())  # type: ignore[arg-type]

    assert result.outsiders_can_view is None and result.note == INTERNAL_ROLES_HINT


def test_a_room_shows_outsiders_only_when_a_person_with_no_internal_role_can_view() -> None:
    guest, closed = _guest(), _open()
    server = fake.guild(channels=[guest, closed])

    assert channel_outsiders(guest, INTERNAL, TEAM).outsiders_can_view is True  # type: ignore[arg-type]
    assert channel_outsiders(closed, INTERNAL, TEAM).outsiders_can_view is False  # type: ignore[arg-type]
    assert server.members[7].bot, "the bot has no internal role and is not an outsider"


def test_a_bot_overwrite_is_not_an_outsider() -> None:
    overwrites = fake.internal_only()
    overwrites[fake.target(discord.User, 40)] = discord.PermissionOverwrite(view_channel=True)
    room = fake.channel(3, "bots", overwrites)
    fake.guild(channels=[room])

    assert channel_outsiders(room, INTERNAL, TEAM).outsiders_can_view is False


def test_a_partial_member_cache_never_certifies_a_room_as_closed() -> None:
    room = _open()
    server = fake.guild(channels=[room], chunked=False)

    assert guild_rooms(server, internal_role_ids=INTERNAL, team_role_ids=TEAM) is None
    assert channel_outsiders(room, INTERNAL, TEAM).outsiders_can_view is True  # type: ignore[arg-type]


def test_rooms_carry_their_parent_and_threads_follow_it() -> None:
    guest, closed = _guest(), _open()
    public = fake.thread(11, 2)
    private = fake.thread(12, 1, private=True)
    server = fake.guild(channels=[guest, closed], threads=[public, private])

    rooms = guild_rooms(server, internal_role_ids=INTERNAL, team_role_ids=TEAM)

    assert rooms is not None
    assert rooms["11"].parent_id == "2" and rooms["11"].facts.contained
    assert rooms["12"].facts.narrow and not rooms["12"].facts.contained
    assert rooms["1"].parent_id is None


def test_an_archived_thread_the_cache_lacks_is_checked_from_the_channel_itself() -> None:
    guest = _guest()
    archived = fake.thread(13, 2, private=True)
    archived.guild = fake.guild(channels=[guest])

    assert channel_outsiders(archived, INTERNAL, TEAM).outsiders_can_view is True


def test_only_a_thread_discord_calls_public_is_public() -> None:
    def thread(kind: discord.ChannelType) -> discord.Thread:
        made = object.__new__(discord.Thread)
        made._type = kind  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
        return made

    assert is_public_thread(thread(discord.ChannelType.public_thread))
    assert is_public_thread(thread(discord.ChannelType.news_thread))
    assert not is_public_thread(thread(discord.ChannelType.private_thread))
    assert not is_public_thread(_open()) and not is_public_thread(None)
