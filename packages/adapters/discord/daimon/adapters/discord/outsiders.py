"""Conservative visibility checks without the privileged members intent."""

from aiohttp import ClientError
from daimon.core.outsiders import INTERNAL_ROLES_HINT, OutsiderVisibility

import discord

MEMBER_OVERWRITE_CAP = 100


async def channel_outsiders(
    channel: discord.abc.GuildChannel, internal_role_ids: tuple[str, ...]
) -> OutsiderVisibility:
    if not internal_role_ids:
        return OutsiderVisibility(note=INTERNAL_ROLES_HINT)
    internal = set(internal_role_ids)
    # Fetch current roles and channel overwrites, including category-synced ones.
    # A stale cache must never certify that a channel excludes outsiders.
    try:
        roles = await channel.guild.fetch_roles()
        fresh_channel = await channel.guild.fetch_channel(channel.id)
        if not isinstance(fresh_channel, discord.abc.GuildChannel):
            return OutsiderVisibility(outsiders_can_view=True)
        channel = fresh_channel
        default = next((role for role in roles if role.id == channel.guild.id), None)
        if default is None or str(channel.guild.id) in internal:
            return OutsiderVisibility(outsiders_can_view=True)
        overwrites = channel.overwrites
        everyone = next((ow for target, ow in overwrites.items() if target.id == default.id), None)
        for role in roles:
            if str(role.id) in internal:
                continue
            base = discord.Permissions(default.permissions.value | role.permissions.value)
            if base.administrator:
                return OutsiderVisibility(outsiders_can_view=True)
            if everyone is not None:
                allow, deny = everyone.pair()
                base.handle_overwrite(allow.value, deny.value)
            if role.id != default.id:
                overwrite = next(
                    (ow for target, ow in overwrites.items() if target.id == role.id), None
                )
                if overwrite is not None:
                    allow, deny = overwrite.pair()
                    base.handle_overwrite(allow.value, deny.value)
            if base.view_channel:
                return OutsiderVisibility(outsiders_can_view=True)
        # Uncached member overwrites are typed Objects; do not discard them.
        members = [
            target.id
            for target, overwrite in channel.overwrites.items()
            if overwrite.view_channel is True
            and (
                isinstance(target, discord.Member)
                or isinstance(target, discord.Object)
                and target.type is discord.User
            )
        ]
        if len(members) > MEMBER_OVERWRITE_CAP:
            return OutsiderVisibility(outsiders_can_view=True)
        for member_id in members:
            member = await channel.guild.fetch_member(member_id)
            if not internal.intersection(str(role.id) for role in member.roles):
                return OutsiderVisibility(outsiders_can_view=True)
    except (discord.HTTPException, ClientError, TimeoutError):
        return OutsiderVisibility(outsiders_can_view=True)
    return OutsiderVisibility(outsiders_can_view=False)
