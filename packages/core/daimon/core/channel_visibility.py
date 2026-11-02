"""Which channels a member can read on their chat platform, from the platform's own facts.

`discord_can_view` is Discord's permission rule for one channel (base role
permissions, then the channel's overwrites). Shells fetch the facts over REST
and cache the answer briefly; anything they cannot verify is hidden.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

DISCORD_ADMINISTRATOR = 1 << 3
DISCORD_VIEW_CHANNEL = 1 << 10
_ROLE, _MEMBER = 0, 1


@dataclass(frozen=True)
class DiscordOverwrite:
    id: str
    type: int
    """0 for a role, 1 for a member."""
    allow: int
    deny: int


@dataclass(frozen=True)
class DiscordMember:
    """A guild member as the permission rule needs them."""

    user_id: str
    role_ids: frozenset[str]
    is_owner: bool = False


def discord_base_permissions(
    member: DiscordMember, *, guild_id: str, role_permissions: Mapping[str, int]
) -> int:
    """The member's guild-wide permissions: @everyone's plus each of their roles'. Pure."""
    permissions = role_permissions.get(guild_id, 0)
    for role_id in member.role_ids:
        permissions |= role_permissions.get(role_id, 0)
    return permissions


def discord_can_view(
    member: DiscordMember,
    *,
    guild_id: str,
    base_permissions: int,
    overwrites: Sequence[DiscordOverwrite],
) -> bool:
    """Whether Discord shows `member` a channel with these overwrites. Pure."""
    if member.is_owner or base_permissions & DISCORD_ADMINISTRATOR:
        return True
    permissions = base_permissions
    everyone = next((o for o in overwrites if o.type == _ROLE and o.id == guild_id), None)
    if everyone is not None:
        permissions = (permissions & ~everyone.deny) | everyone.allow
    allow = deny = 0
    for overwrite in overwrites:
        if overwrite.type == _ROLE and overwrite.id in member.role_ids:
            allow |= overwrite.allow
            deny |= overwrite.deny
    permissions = (permissions & ~deny) | allow
    own = next(
        (o for o in overwrites if o.type == _MEMBER and o.id == member.user_id),
        None,
    )
    if own is not None:
        permissions = (permissions & ~own.deny) | own.allow
    return bool(permissions & DISCORD_VIEW_CHANNEL)


def discord_visible_channels(
    member: DiscordMember,
    *,
    guild_id: str,
    role_permissions: Mapping[str, int],
    channel_overwrites: Mapping[str, Sequence[DiscordOverwrite]],
    channel_ids: Collection[str],
) -> frozenset[str]:
    """The ids among `channel_ids` the member can view; a channel the guild lacks is not. Pure."""
    base = discord_base_permissions(member, guild_id=guild_id, role_permissions=role_permissions)
    return frozenset(
        channel_id
        for channel_id in channel_ids
        if channel_id in channel_overwrites
        and discord_can_view(
            member,
            guild_id=guild_id,
            base_permissions=base,
            overwrites=channel_overwrites[channel_id],
        )
    )
