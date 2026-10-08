"""A Discord member's roles, read live for channel admin rights outside a turn."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.channel_admin_roles import member_roles, stored_member_roles
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.channel_admins import GroupLookupFailed, GroupMembersCache


def _client(*answers: Any) -> MagicMock:
    client = MagicMock()
    client.http.get_member = AsyncMock(side_effect=answers)
    return client


def _runtime() -> DiscordRuntime:
    return MagicMock(group_members=GroupMembersCache(ttl_s=0, failure_ttl_s=0))


async def test_a_member_s_roles_are_read_and_one_who_left_holds_none() -> None:
    gone = discord.NotFound(MagicMock(status=404), "Unknown Member")
    client = _client({"roles": ["11", 22]}, gone)
    roles = member_roles(_runtime(), client, "1")

    assert await roles("2") == frozenset({"11", "22"})
    assert await roles("2") == frozenset(), "left the guild: no roles"
    client.http.get_member.assert_awaited_with(1, 2)


async def test_a_failed_read_raises_so_it_grants_nothing() -> None:
    roles = member_roles(_runtime(), _client(discord.HTTPException(MagicMock(status=500), "")), "1")

    with pytest.raises(GroupLookupFailed):
        await roles("2")


def test_only_discord_gets_the_lookup() -> None:
    lookup = stored_member_roles(_runtime(), _client())

    assert lookup("discord", "1") is not None
    assert lookup("slack", "T1") is None
