"""A fictional Discord server built from discord.py-shaped fakes, for the room check."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import discord

GUILD = 4000
CORE, FRIEND = 4101, 4102
VIEW = discord.Permissions(view_channel=True)


def role(role_id: int, *, view: bool = False) -> Any:
    fake = MagicMock(spec=discord.Role)
    fake.id = role_id
    fake.permissions = discord.Permissions(view_channel=view)
    fake.is_default.return_value = role_id == GUILD
    return fake


def member(user_id: int, roles: list[Any], *, bot: bool = False, timed_out: bool = False) -> Any:
    fake = MagicMock(spec=discord.Member)
    fake.id, fake.bot, fake.roles = user_id, bot, [role(GUILD), *roles]
    fake.is_timed_out.return_value = timed_out
    fake.guild_permissions = discord.Permissions()
    return fake


def target(kind: type, target_id: int) -> discord.Object:
    return discord.Object(id=target_id, type=kind)  # type: ignore[arg-type]


def channel(
    channel_id: int,
    name: str,
    overwrites: dict[discord.Object, discord.PermissionOverwrite],
    *,
    kind: discord.ChannelType = discord.ChannelType.text,
) -> Any:
    fake = MagicMock(spec=discord.TextChannel)
    fake.id, fake.name, fake.type, fake.overwrites = channel_id, name, kind, overwrites
    return fake


def thread(thread_id: int, parent_id: int, *, private: bool = False, name: str = "thread") -> Any:
    fake = MagicMock(spec=discord.Thread)
    fake.id, fake.parent_id, fake.name = thread_id, parent_id, name
    fake.type = discord.ChannelType.private_thread if private else discord.ChannelType.public_thread
    return fake


def internal_only() -> dict[discord.Object, discord.PermissionOverwrite]:
    return {
        target(discord.Role, GUILD): discord.PermissionOverwrite(view_channel=False),
        target(discord.Role, CORE): discord.PermissionOverwrite(view_channel=True),
        target(discord.Role, FRIEND): discord.PermissionOverwrite(view_channel=True),
    }


def guild(*, channels: list[Any], threads: list[Any] | None = None, chunked: bool = True) -> Any:
    """Five core members, one friend, one outsider and a bot. Roles give core and friend a
    base view; @everyone has none."""
    fake = MagicMock(spec=discord.Guild)
    core, friend = role(CORE, view=True), role(FRIEND, view=True)
    fake.id, fake.owner_id, fake.chunked, fake.name = GUILD, 1, chunked, "Fictional"
    fake.roles = [role(GUILD), core, friend]
    fake.members = [
        *(member(10 + n, [core]) for n in range(5)),
        member(20, [friend]),
        member(30, []),
        member(40, [], bot=True),
    ]
    fake.channels = channels
    fake.threads = threads or []
    for each in (*channels, *fake.threads):
        each.guild = fake
    return fake
