"""Discord channel admins outside a chat turn: a member's roles, read live.

A turn carries its sender's roles, and the ones a grant matched are stored. A
budget notice or an ask-a-human DM goes out later, so each stored role match
is checked against the member's current roles (`GroupMembers` goes from user
to roles on Discord), through the runtime's short cache. Discord lists a
role's members only with a privileged intent, so one member is read at a time.
A failed read grants nothing; a member who left holds no roles.
"""

from __future__ import annotations

from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.channel_admins import GroupLookupFailed, GroupMembers, GroupMembersFor

import discord


async def _fetch_roles(client: discord.Client, guild_id: str, user_id: str) -> frozenset[str]:
    try:
        member = await client.http.get_member(int(guild_id), int(user_id))
    except discord.NotFound:
        return frozenset()
    except (discord.HTTPException, ValueError) as exc:
        raise GroupLookupFailed(type(exc).__name__) from exc
    return frozenset(str(role) for role in member["roles"])


def member_roles(runtime: DiscordRuntime, client: discord.Client, guild_id: str) -> GroupMembers:
    """A guild member's current role ids, by user id, through the runtime's cache."""

    async def roles(user_id: str) -> frozenset[str]:
        return await runtime.group_members.members(
            ("discord", guild_id, user_id), lambda: _fetch_roles(client, guild_id, user_id)
        )

    return roles


def stored_member_roles(runtime: DiscordRuntime, client: discord.Client) -> GroupMembersFor:
    """The live re-check of stored roles, for a message sent outside its recipient's turn."""
    return lambda platform, guild_id: (
        member_roles(runtime, client, guild_id) if platform == "discord" else None
    )


__all__ = ["member_roles", "stored_member_roles"]
