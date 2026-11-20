"""The room check: who can see each room of a fictional server, one person at a time."""

from __future__ import annotations

from daimon.core.audience_facts import (
    UNKNOWN_FACTS,
    AudienceChannel,
    AudienceMember,
    RoomFacts,
    compute_room_facts,
)
from daimon.core.channel_visibility import DISCORD_VIEW_CHANNEL, DiscordOverwrite

GUILD, CORE, FRIEND = "1000", "2001", "2002"
VIEW = DISCORD_VIEW_CHANNEL


def _role(role_id: str, *, allow: int = 0, deny: int = 0) -> DiscordOverwrite:
    return DiscordOverwrite(role_id, 0, allow, deny)


def _person(user_id: str, *, allow: int = 0, deny: int = 0) -> DiscordOverwrite:
    return DiscordOverwrite(user_id, 1, allow, deny)


# Five core members, one friend, two people with no internal role (one timed out), one bot.
MEMBERS = [
    *(AudienceMember(f"u{n}", frozenset({CORE})) for n in range(1, 6)),
    AudienceMember("friend", frozenset({FRIEND})),
    AudienceMember("outsider", frozenset()),
    AudienceMember("quiet", frozenset(), timed_out=True),
    AudienceMember("bot", frozenset(), is_bot=True),
]
ROLE_PERMISSIONS = {GUILD: 0, CORE: VIEW, FRIEND: VIEW}
# @everyone's own overwrite strips the base view, so each internal role is allowed back in.
INTERNAL_ONLY = (_role(GUILD, deny=VIEW), _role(CORE, allow=VIEW), _role(FRIEND, allow=VIEW))
CLOSED = (_role(GUILD, deny=VIEW),)


def _facts(
    *channels: AudienceChannel, members: list[AudienceMember] = MEMBERS
) -> dict[str, RoomFacts]:
    return compute_room_facts(
        guild_id=GUILD,
        members=members,
        role_permissions=ROLE_PERMISSIONS,
        channels=list(channels),
        internal_role_ids=[CORE, FRIEND],
        team_role_ids=[CORE],
    )


def _room(
    channel_id: str, name: str, *overwrites: DiscordOverwrite, type: int = 0
) -> AudienceChannel:
    return AudienceChannel(channel_id, type, name, overwrites=overwrites)


def test_a_room_only_internal_people_see_is_open() -> None:
    facts = _facts(_room("101", "planning", *INTERNAL_ONLY))["101"]

    assert facts == RoomFacts(
        contained=False, narrow=False, outsiders_count=0, team_shut_out_count=0
    )


def test_one_outsider_contains_the_room_and_a_bot_does_not() -> None:
    facts = _facts(
        _room("102", "guest", *INTERNAL_ONLY, _person("outsider", allow=VIEW)),
        _room("104", "bots", *INTERNAL_ONLY, _person("bot", allow=VIEW)),
    )

    assert facts["102"].contained and facts["102"].outsiders_count == 1
    assert not facts["104"].contained and facts["104"].outsiders_count == 0, "bots never count"


def test_a_timed_out_outsider_still_sees_the_room() -> None:
    facts = _facts(_room("105", "quiet", *INTERNAL_ONLY, _person("quiet", allow=VIEW)))["105"]

    assert facts.contained and facts.outsiders_count == 1


def test_client_in_the_name_contains_a_room_with_no_outsider() -> None:
    facts = _facts(_room("103", "Acme-CLIENT", *INTERNAL_ONLY))["103"]

    assert facts.contained and facts.outsiders_count == 0


def test_two_team_members_shut_out_is_not_narrow_and_three_is() -> None:
    def shut_out(allowed: tuple[str, ...]) -> tuple[DiscordOverwrite, ...]:
        return (
            *CLOSED,
            _role(FRIEND, allow=VIEW),
            _role(CORE, deny=VIEW),
            *(_person(user, allow=VIEW) for user in allowed),
        )

    facts = _facts(
        _room("106", "two-out", *shut_out(("u1", "u2", "u3"))),
        _room("107", "three-out", *shut_out(("u1", "u2"))),
    )

    assert facts["106"].team_shut_out_count == 2 and not facts["106"].narrow
    assert facts["107"].team_shut_out_count == 3 and facts["107"].narrow
    assert not facts["107"].contained, "a narrow room can still be open of outsiders"


def test_a_category_synced_room_follows_the_overwrites_it_carries() -> None:
    category = (*CLOSED, _role(CORE, deny=VIEW), _role(FRIEND, allow=VIEW))
    facts = _facts(
        AudienceChannel("200", 4, "leads", overwrites=category),
        _room("108", "synced", *category),
    )

    assert "200" not in facts, "a category is not a room"
    assert facts["108"].narrow and facts["108"].team_shut_out_count == 5


def test_a_forum_post_and_voice_text_use_their_own_channels_permissions() -> None:
    facts = _facts(
        _room("109", "forum", *INTERNAL_ONLY, _person("outsider", allow=VIEW), type=15),
        AudienceChannel("110", 11, "a post", parent_id="109"),
        _room("111", "voice", *INTERNAL_ONLY, type=2),
        _room("112", "stage", *INTERNAL_ONLY, _person("outsider", allow=VIEW), type=13),
    )

    assert facts["109"] == facts["110"] and facts["110"].contained
    assert not facts["111"].contained, "voice text follows the voice room, not any other"
    assert facts["112"].contained


def test_a_public_thread_takes_its_parents_facts() -> None:
    facts = _facts(
        _room("101", "planning", *INTERNAL_ONLY),
        AudienceChannel("113", 11, "thread", parent_id="101"),
    )

    assert facts["113"] == facts["101"]


def test_a_private_thread_is_contained_as_its_parent_and_always_narrow() -> None:
    facts = _facts(
        _room("102", "guest", *INTERNAL_ONLY, _person("outsider", allow=VIEW)),
        _room("101", "planning", *INTERNAL_ONLY),
        AudienceChannel("114", 12, "internal only", parent_id="102"),
        AudienceChannel("115", 12, "plain", parent_id="101"),
    )

    assert facts["114"].contained and facts["114"].narrow, "an upper bound: the parent's viewers"
    assert not facts["115"].contained and facts["115"].narrow


def test_missing_facts_report_contained_and_narrow() -> None:
    facts = _facts(
        AudienceChannel("116", 11, "orphan", parent_id="999"),
        AudienceChannel("117", 14, "an unknown channel type"),
        AudienceChannel("118", 12, "orphan private", parent_id="999"),
    )

    assert facts == {"116": UNKNOWN_FACTS, "117": UNKNOWN_FACTS, "118": UNKNOWN_FACTS}
    assert UNKNOWN_FACTS.contained and UNKNOWN_FACTS.narrow and not UNKNOWN_FACTS.known


def test_people_with_the_same_roles_are_counted_one_by_one() -> None:
    many = [AudienceMember(f"p{n}", frozenset()) for n in range(50)]
    overwrites = (_role(GUILD, allow=VIEW), _person("p7", deny=VIEW))
    facts = _facts(_room("101", "open", *overwrites), members=[*MEMBERS, *many])["101"]

    assert facts.outsiders_count == 2 + 49, "a person's own deny is not shared with their group"
    assert facts.contained
