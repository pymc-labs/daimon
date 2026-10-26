"""Gateway membership evidence and live Discord SDK fallback."""

from __future__ import annotations

from uuid import UUID

from daimon.adapters.discord.checks import is_member_guild_admin, member_role_ids
from daimon.core.membership import CachedMembershipResolver, Membership

import discord


def member_facts(member: discord.Member) -> Membership:
    return Membership(
        is_member=True,
        is_bot=member.bot,
        role_ids=tuple(member_role_ids(member)),
        is_admin=is_member_guild_admin(member, guild_owner_id=member.guild.owner_id),
    )


class GatewayMembershipResolver(CachedMembershipResolver):
    """Only event facts are observed; SDK member cache entries are never trusted."""

    def __init__(self, client: discord.Client):
        self._client = client
        super().__init__(self._fetch_member)

    async def _fetch_member(self, tenant_id: UUID, guild_id: str, user_id: str) -> Membership:
        del tenant_id
        guild = self._client.get_guild(int(guild_id))
        if guild is not None and guild.unavailable:
            return Membership()
        # A live member fetch alone would still compute permissions from
        # the SDK guild's untimestamped role/owner cache. Fetch guild metadata
        # even on a cold SDK cache; a guild failure is unknown, not user absence.
        current_guild = await self._client.fetch_guild(int(guild_id))
        try:
            return member_facts(await current_guild.fetch_member(int(user_id)))
        except discord.NotFound:
            return Membership(is_member=False)
