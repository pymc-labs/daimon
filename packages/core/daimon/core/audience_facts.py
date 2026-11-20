"""Who is in each Discord room: the two facts Daimon keeps about a room's audience.

Bots never count as people. A person is internal when they hold a role in the server's
internal roles, and a team member when they hold a role in its team roles (a subset).
For each room:

- **contained**: a person who is not internal can view it, or "client" is in its name,
  or the facts are unknown.
- **narrow**: three or more team members cannot read it (view it and read its history),
  or it is a private thread, or the facts are unknown.

A private thread takes "contained" from its parent room and is always narrow. A public
thread and a forum post take both facts from their parent. A voice or stage room, and
its text, use its own permissions. Permissions are Discord's own rule
(`daimon.core.channel_visibility`), one person at a time; an owner or administrator
holds every permission. "Contained" counts viewers (View Channel), "narrow" counts
readers (View Channel and Read Message History). Pure: adapters supply the facts.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

from daimon.core.channel_visibility import (
    DISCORD_ADMINISTRATOR,
    DISCORD_READ_HISTORY,
    DISCORD_VIEW_CHANNEL,
    DiscordMember,
    DiscordOverwrite,
    discord_base_permissions,
    discord_channel_permissions,
)

#: A room that shuts out this many team members or more is narrow.
NARROW_TEAM_THRESHOLD = 3

# Discord channel type numbers.
_CATEGORY = 4
_PRIVATE_THREAD = 12
_THREADS = frozenset({10, 11, 12})
_ROOMS = frozenset({0, 2, 5, 13, 15, 16})
"""Text, voice, announcement, stage, forum and media rooms: each has its own overwrites."""


@dataclass(frozen=True)
class AudienceMember:
    """A server member as the room check needs them."""

    user_id: str
    role_ids: frozenset[str]
    is_bot: bool = False
    is_owner: bool = False
    timed_out: bool = False


@dataclass(frozen=True)
class AudienceChannel:
    """A channel or thread of the server."""

    id: str
    type: int
    name: str
    parent_id: str | None = None
    overwrites: tuple[DiscordOverwrite, ...] = ()


@dataclass(frozen=True)
class RoomFacts:
    """A room's audience facts. `known` is explicit: a room read back from the database
    is known and has no counts; the counts exist only on freshly computed facts."""

    contained: bool
    narrow: bool
    outsiders_count: int | None = None
    team_shut_out_count: int | None = None
    known: bool = True


#: What an unknown room reports: contained and narrow.
UNKNOWN_FACTS = RoomFacts(contained=True, narrow=True, known=False)


def is_client_name(name: str) -> bool:
    return "client" in name.casefold()


def compute_room_facts(
    *,
    guild_id: str,
    members: Iterable[AudienceMember],
    role_permissions: Mapping[str, int],
    channels: Sequence[AudienceChannel],
    internal_role_ids: Collection[str],
    team_role_ids: Collection[str],
) -> dict[str, RoomFacts]:
    """The facts of each room and thread in `channels`; categories are left out.

    `role_permissions` maps every role id, @everyone's (the server id) included, to its
    permission bits. A thread whose parent is not in `channels`, and a channel of a type
    this rule does not know, is unknown.
    """
    internal = frozenset(internal_role_ids)
    team = frozenset(team_role_ids) & internal
    people = [
        DiscordMember(
            m.user_id, m.role_ids - {guild_id}, is_owner=m.is_owner, timed_out=m.timed_out
        )
        for m in members
        if not m.is_bot
    ]
    outsiders = [p for p in people if not p.role_ids & internal]
    team_members = [p for p in people if p.role_ids & team]
    own: dict[str, RoomFacts] = {}

    def room_facts(channel: AudienceChannel) -> RoomFacts:
        overwrites = channel.overwrites
        outside = _count(guild_id, role_permissions, outsiders, overwrites, _VIEW)
        shut_out = len(team_members) - _count(
            guild_id, role_permissions, team_members, overwrites, _READ
        )
        return RoomFacts(
            contained=outside > 0 or is_client_name(channel.name),
            narrow=shut_out >= NARROW_TEAM_THRESHOLD,
            outsiders_count=outside,
            team_shut_out_count=shut_out,
        )

    for channel in channels:
        if channel.type in _ROOMS:
            own[channel.id] = room_facts(channel)
    facts: dict[str, RoomFacts] = {}
    for channel in channels:
        if channel.type == _CATEGORY:
            continue
        if channel.type in _ROOMS:
            facts[channel.id] = own[channel.id]
        elif channel.type in _THREADS:
            parent = own.get(channel.parent_id or "")
            if parent is None:
                facts[channel.id] = UNKNOWN_FACTS
            elif channel.type == _PRIVATE_THREAD:
                facts[channel.id] = RoomFacts(
                    contained=parent.contained,
                    narrow=True,
                    outsiders_count=parent.outsiders_count,
                    team_shut_out_count=parent.team_shut_out_count,
                )
            else:
                facts[channel.id] = parent
        else:
            facts[channel.id] = UNKNOWN_FACTS
    return facts


_VIEW = DISCORD_VIEW_CHANNEL
_READ = DISCORD_VIEW_CHANNEL | DISCORD_READ_HISTORY
"""Permission bits a person must hold: to view a room, and to read it."""


def _count(
    guild_id: str,
    role_permissions: Mapping[str, int],
    people: Sequence[DiscordMember],
    overwrites: Sequence[DiscordOverwrite],
    needed: int,
) -> int:
    """How many of `people` hold every bit of `needed` in a channel with these overwrites.

    People who share roles, owner and timeout standing and have no overwrite of their
    own get one answer between them, so a large server costs one check per role set.
    """
    named = {o.id for o in overwrites if o.type == 1}
    groups: dict[tuple[frozenset[str], bool, bool], list[DiscordMember]] = defaultdict(list)
    count = 0
    for person in people:
        if person.user_id in named:
            count += _holds(person, guild_id, role_permissions, overwrites, needed)
        else:
            groups[(person.role_ids, person.is_owner, person.timed_out)].append(person)
    for members in groups.values():
        if _holds(members[0], guild_id, role_permissions, overwrites, needed):
            count += len(members)
    return count


def _holds(
    person: DiscordMember,
    guild_id: str,
    role_permissions: Mapping[str, int],
    overwrites: Sequence[DiscordOverwrite],
    needed: int,
) -> bool:
    permissions = discord_channel_permissions(
        person,
        guild_id=guild_id,
        base_permissions=discord_base_permissions(
            person, guild_id=guild_id, role_permissions=role_permissions
        ),
        overwrites=overwrites,
    )
    return bool(permissions & DISCORD_ADMINISTRATOR) or permissions & needed == needed
