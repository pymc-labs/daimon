"""The room check on Discord's gateway cache: who can see a room, one person at a time.

A thin converter. The rule lives in `daimon.core.audience_facts`; this module only turns
a discord.py guild into its inputs. The cache is complete only when every member is
cached (`guild.chunked`); until then every room is unknown.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from daimon.core.audience_facts import (
    AudienceChannel,
    AudienceMember,
    RoomFacts,
    compute_room_facts,
)
from daimon.core.channel_visibility import DiscordOverwrite
from daimon.core.outsiders import INTERNAL_ROLES_HINT, OutsiderVisibility
from daimon.core.stores.audience_facts import StoredRoom

import discord

_ROLE, _MEMBER = 0, 1


def _overwrites(channel: discord.abc.GuildChannel) -> tuple[DiscordOverwrite, ...]:
    found: list[DiscordOverwrite] = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        is_role = isinstance(target, discord.Role) or (
            isinstance(target, discord.Object) and target.type is discord.Role
        )
        found.append(
            DiscordOverwrite(str(target.id), _ROLE if is_role else _MEMBER, allow.value, deny.value)
        )
    return tuple(found)


def audience_channel(channel: discord.abc.GuildChannel | discord.Thread) -> AudienceChannel:
    if isinstance(channel, discord.Thread):
        return AudienceChannel(
            str(channel.id),
            channel.type.value,
            channel.name,
            parent_id=str(channel.parent_id),
        )
    return AudienceChannel(
        str(channel.id), channel.type.value, channel.name, overwrites=_overwrites(channel)
    )


def is_public_thread(channel: object) -> bool:
    """Whether Discord says this channel is a public thread (a forum post and an announcement
    thread included). Anything else, a private thread or a type not known, is False."""
    return isinstance(channel, discord.Thread) and channel.type in (
        discord.ChannelType.public_thread,
        discord.ChannelType.news_thread,
    )


def audience_member(guild: discord.Guild, member: discord.Member) -> AudienceMember:
    return AudienceMember(
        user_id=str(member.id),
        role_ids=frozenset(str(role.id) for role in member.roles if not role.is_default()),
        is_bot=member.bot,
        is_owner=member.id == guild.owner_id,
        timed_out=member.is_timed_out(),
    )


def guild_rooms(
    guild: discord.Guild,
    *,
    internal_role_ids: tuple[str, ...],
    team_role_ids: tuple[str, ...],
    extra: Iterable[discord.abc.GuildChannel | discord.Thread] = (),
    roles: Sequence[discord.Role] | None = None,
    guild_channels: Sequence[discord.abc.GuildChannel] | None = None,
    members: Sequence[discord.Member] | None = None,
) -> dict[str, StoredRoom] | None:
    """Each room's facts with its parent, from the cache; None while the cache is partial.

    `extra` names channels the cache may lack, such as an archived thread. `roles` and
    `guild_channels` replace the cached roles and channels (not threads) with a fresh
    REST read. `members` is a full member list the caller vouches for; only then is
    `guild.chunked` (a count comparison a missed join can break) not consulted.
    """
    if members is None and not guild.chunked:
        return None
    cached_channels = guild.channels if guild_channels is None else guild_channels
    seen = {c.id for c in (*cached_channels, *guild.threads)}
    channels = [
        audience_channel(c)
        for c in (*cached_channels, *guild.threads, *(e for e in extra if e.id not in seen))
    ]
    facts = compute_room_facts(
        guild_id=str(guild.id),
        members=[
            audience_member(guild, member)
            for member in (guild.members if members is None else members)
        ],
        role_permissions={
            str(role.id): role.permissions.value
            for role in (guild.roles if roles is None else roles)
        },
        channels=channels,
        internal_role_ids=internal_role_ids,
        team_role_ids=team_role_ids,
    )
    parents = {c.id: c.parent_id for c in channels}
    return {
        channel_id: StoredRoom(room, parents.get(channel_id)) for channel_id, room in facts.items()
    }


def channel_outsiders(
    channel: discord.abc.GuildChannel | discord.Thread,
    internal_role_ids: tuple[str, ...],
    team_role_ids: tuple[str, ...] = (),
) -> OutsiderVisibility:
    """Whether anyone who is not internal can view the channel, from the cache.

    True when the cache cannot say, so a partial cache never certifies a room as closed.
    """
    if not internal_role_ids:
        return OutsiderVisibility(note=INTERNAL_ROLES_HINT)
    rooms = guild_rooms(
        channel.guild,
        internal_role_ids=internal_role_ids,
        team_role_ids=team_role_ids,
        extra=(channel,),
    )
    room = None if rooms is None else rooms.get(str(channel.id))
    return OutsiderVisibility(outsiders_can_view=_has_outsiders(room.facts if room else None))


def _has_outsiders(facts: RoomFacts | None) -> bool:
    if facts is None or not facts.known:
        return True
    if facts.outsiders_count is None:
        return facts.contained
    return facts.outsiders_count > 0
