"""Explicit platform membership facts for offline fixtures."""

from __future__ import annotations

from uuid import UUID

from daimon.core.membership import CachedMembershipResolver, Membership


class FixtureMembershipResolver(CachedMembershipResolver):
    """Resolve only the fixture's declared platform users; others are absent."""

    def __init__(self, *user_ids: str) -> None:
        super().__init__(self._fetch_member)
        self.members = {user_id: Membership(is_member=True) for user_id in user_ids}

    async def _fetch_member(self, tenant_id: UUID, guild_id: str, user_id: str) -> Membership:
        return self.members.get(user_id, Membership(is_member=False))
