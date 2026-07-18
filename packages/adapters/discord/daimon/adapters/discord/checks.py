"""Gating decorators for slash command handlers.

``require_registered_guild`` verifies the invoking guild is in the workspace
registry (DB check).  ``require_manage_guild`` verifies the invoking
user has Discord-native manage_guild (or administrator, or is the owner).

Both decorators consume the interaction response on rejection (ephemeral error)
and return early.  The happy path falls through to the wrapped handler, which
calls ``defer()`` as its first action.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Callable, Coroutine
from typing import Any, Concatenate, ParamSpec, cast

from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.tenants import get_tenant

import discord
from discord import Interaction
from discord.ext import commands

P = ParamSpec("P")


def is_member_guild_admin(member: discord.Member, *, guild_owner_id: int | None) -> bool:
    """Return True if the member is a guild admin by Discord-native permissions.

    Checks: guild owner, administrator permission, or manage_guild permission.
    Pure function — no I/O, no DB access.
    """
    if guild_owner_id is not None and member.id == guild_owner_id:
        return True
    return member.guild_permissions.administrator or member.guild_permissions.manage_guild


def is_guild_admin(interaction: Interaction[commands.Bot]) -> bool:
    """Discord-native admin check: owner OR manage_guild OR administrator.

    Returns False for DM contexts (user is not a Member).
    """
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    guild = interaction.guild
    owner_id = guild.owner_id if guild is not None else None
    return is_member_guild_admin(user, guild_owner_id=owner_id)


async def resolve_tenant_for_interaction(
    bot: commands.Bot, interaction: Interaction[commands.Bot]
) -> uuid.UUID | None:
    """Resolve the tenant_id owning the interaction's guild, per-interaction.

    Returns None for a DM context (no guild_id) or an unprovisioned guild.
    The runtime no longer carries a boot-time tenant_id; each
    interaction resolves its own guild's tenant.
    """
    if interaction.guild_id is None:
        return None
    runtime = cast(DiscordRuntime, bot.runtime)  # type: ignore[attr-defined]  # DaimonBot.runtime not on Bot type
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
    async with runtime.sessionmaker() as session:
        row = await get_tenant(session, tenant_id)
    return tenant_id if row is not None else None


def require_registered_guild(  # noqa: UP047  -- ParamSpec used for decorator generics; PEP 695 syntax not adopted here
    func: Callable[Concatenate[Any, Interaction[commands.Bot], P], Coroutine[Any, Any, None]],
) -> Callable[Concatenate[Any, Interaction[commands.Bot], P], Coroutine[Any, Any, None]]:
    """Reject interaction if the guild is not in the tenants table."""

    @functools.wraps(func)
    async def wrapper(
        self: Any,  # noqa: ANN401
        interaction: Interaction[commands.Bot],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command is only available in a server.",
                ephemeral=True,
            )
            return
        tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
        if tenant_id is None:
            await interaction.response.send_message(
                "This server is not registered. Ask an operator to register it.",
                ephemeral=True,
            )
            return
        await func(self, interaction, *args, **kwargs)

    return wrapper


def require_manage_guild(  # noqa: UP047  -- ParamSpec used for decorator generics; PEP 695 syntax not adopted here
    func: Callable[Concatenate[Any, Interaction[commands.Bot], P], Coroutine[Any, Any, None]],
) -> Callable[Concatenate[Any, Interaction[commands.Bot], P], Coroutine[Any, Any, None]]:
    """Reject interaction if the user lacks Discord-native manage_guild (or administrator / owner).

    DB-role-free: checks Discord permissions only, not the daimon accounts.role column.
    Rejects plain Users (DM context) with the same ephemeral message.
    """

    @functools.wraps(func)
    async def wrapper(
        self: Any,  # noqa: ANN401
        interaction: Interaction[commands.Bot],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        if not is_guild_admin(interaction):
            await interaction.response.send_message(
                "Changing my setup needs Manage Server — ask a server admin to use /agent-setup",
                ephemeral=True,
            )
            return
        await func(self, interaction, *args, **kwargs)

    return wrapper
