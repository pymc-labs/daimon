"""Outsider checks must retain uncached overwrites and fail conservatively."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.outsiders import MEMBER_OVERWRITE_CAP, channel_outsiders
from daimon.core.outsiders import INTERNAL_ROLES_HINT

INTERNAL = "900000000000000001"


def _role(role_id: int, *, view: bool = False, admin: bool = False) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.permissions = discord.Permissions(view_channel=view, administrator=admin)
    return role


def _channel(*, everyone_view: bool = False) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 123
    channel.guild.id = 100
    channel.guild.fetch_channel = AsyncMock(return_value=channel)
    channel.guild.fetch_roles = AsyncMock(
        return_value=[
            _role(100, view=everyone_view),
            _role(int(INTERNAL), view=True),
            _role(200),
        ]
    )
    channel.guild.fetch_member = AsyncMock()
    channel.overwrites = {}
    return channel


async def test_without_internal_roles_does_not_claim_visibility() -> None:
    channel = _channel(everyone_view=True)
    result = await channel_outsiders(channel, ())
    assert result.outsiders_can_view is None
    assert result.note == INTERNAL_ROLES_HINT
    channel.guild.fetch_roles.assert_not_awaited()


@pytest.mark.parametrize("everyone_view", [True, False])
async def test_everyone_or_internal_only(everyone_view: bool) -> None:
    result = await channel_outsiders(_channel(everyone_view=everyone_view), (INTERNAL,))
    assert result.outsiders_can_view is everyone_view


async def test_non_internal_role_channel_allow_beats_everyone_deny() -> None:
    channel = _channel(everyone_view=True)
    channel.overwrites = {
        discord.Object(id=100, type=discord.Role): discord.PermissionOverwrite(view_channel=False),
        discord.Object(id=200, type=discord.Role): discord.PermissionOverwrite(view_channel=True),
    }
    assert (await channel_outsiders(channel, (INTERNAL,))).outsiders_can_view is True


async def test_synced_overwrites_are_taken_from_fresh_channel() -> None:
    channel = _channel(everyone_view=True)
    fresh = _channel(everyone_view=True)
    fresh.overwrites = {
        discord.Object(id=100, type=discord.Role): discord.PermissionOverwrite(view_channel=False),
        discord.Object(id=int(INTERNAL), type=discord.Role): discord.PermissionOverwrite(
            view_channel=True
        ),
    }
    channel.guild.fetch_channel.return_value = fresh
    assert (await channel_outsiders(channel, (INTERNAL,))).outsiders_can_view is False


@pytest.mark.parametrize("internal", [True, False])
async def test_uncached_member_allow_fetches_roles(internal: bool) -> None:
    channel = _channel()
    channel.overwrites = {
        discord.Object(id=300, type=discord.User): discord.PermissionOverwrite(view_channel=True),
    }
    channel.guild.fetch_member.return_value.roles = [_role(int(INTERNAL) if internal else 200)]
    result = await channel_outsiders(channel, (INTERNAL,))
    assert result.outsiders_can_view is not internal
    channel.guild.fetch_member.assert_awaited_once_with(300)


async def test_member_deny_does_not_grant_visibility() -> None:
    channel = _channel()
    channel.overwrites = {
        discord.Object(id=300, type=discord.User): discord.PermissionOverwrite(view_channel=False),
    }
    assert (await channel_outsiders(channel, (INTERNAL,))).outsiders_can_view is False
    channel.guild.fetch_member.assert_not_awaited()


async def test_member_cap_flags_without_fetching_members() -> None:
    channel = _channel()
    channel.overwrites = {
        discord.Object(id=300 + i, type=discord.User): discord.PermissionOverwrite(
            view_channel=True
        )
        for i in range(MEMBER_OVERWRITE_CAP + 1)
    }
    assert (await channel_outsiders(channel, (INTERNAL,))).outsiders_can_view is True
    channel.guild.fetch_member.assert_not_awaited()


async def test_failed_member_lookup_flags() -> None:
    channel = _channel()
    channel.overwrites = {
        discord.Object(id=300, type=discord.User): discord.PermissionOverwrite(view_channel=True)
    }
    channel.guild.fetch_member.side_effect = discord.Forbidden(MagicMock(status=403), "forbidden")
    assert (await channel_outsiders(channel, (INTERNAL,))).outsiders_can_view is True


async def test_non_internal_administrator_bypasses_deny() -> None:
    channel = _channel()
    channel.guild.fetch_roles.return_value.append(_role(500, admin=True))
    channel.overwrites = {
        discord.Object(id=500, type=discord.Role): discord.PermissionOverwrite(view_channel=False)
    }
    assert (await channel_outsiders(channel, (INTERNAL,))).outsiders_can_view is True


async def test_exact_member_cap_checks_every_grant() -> None:
    channel = _channel()
    channel.overwrites = {
        discord.Object(id=300 + i, type=discord.User): discord.PermissionOverwrite(
            view_channel=True
        )
        for i in range(MEMBER_OVERWRITE_CAP)
    }
    channel.guild.fetch_member.return_value.roles = [_role(int(INTERNAL))]
    assert (await channel_outsiders(channel, (INTERNAL,))).outsiders_can_view is False
    assert channel.guild.fetch_member.await_count == MEMBER_OVERWRITE_CAP
