"""`/model` shows or switches the model a Discord conversation thread runs on."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.commands.model import ModelCog
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.thread_models import get_thread_model
from daimon.testing.factories import make_tenant
from daimon.testing.turn_fakes import deps_for
from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_PARENT_ID = 555
_THREAD_ID = 777


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> DiscordRuntime:
    return DiscordRuntime(
        settings=MagicMock(),
        anthropic=MagicMock(),  # pyright: ignore[reportArgumentType]  # /model makes no MA call
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=replace(deps_for(), turn_path=None, membership_resolver=None),
    )


def _interaction(runtime: DiscordRuntime, *, guild_id: int, in_thread: bool = True) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.owner_id = 1
    user = MagicMock(spec=discord.Member)
    user.id = 42
    user.guild_permissions.administrator = False
    user.guild_permissions.manage_guild = False
    if in_thread:
        channel = MagicMock(spec=discord.Thread)
        channel.id = _THREAD_ID
        channel.parent_id = _PARENT_ID
        channel.parent = MagicMock(spec=discord.TextChannel)
        channel.parent.category_id = None
    else:
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = _PARENT_ID
    interaction = MagicMock()
    interaction.client.runtime = runtime
    interaction.guild_id = guild_id
    interaction.guild = guild
    interaction.user = user
    interaction.channel = channel
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


async def _registered_guild(
    db_session_factory: async_sessionmaker[AsyncSession],
    guild_id: int,
    policy: TenantAccessPolicy | None = None,
) -> None:
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(guild_id))
        if policy is not None:
            await set_access_policy(session, tenant_id=tenant.id, policy=policy)
        await session.commit()


async def _run(interaction: MagicMock, choice: str | None) -> str:
    cog = ModelCog(MagicMock())
    picked = None if choice is None else app_commands.Choice(name=choice, value=choice)
    await cog.model.callback(cog, interaction, picked)  # pyright: ignore[reportArgumentType, reportCallIssue]
    interaction.edit_original_response.assert_awaited_once()
    return interaction.edit_original_response.call_args.kwargs["content"]


async def _stored(
    db_session_factory: async_sessionmaker[AsyncSession], guild_id: int
) -> str | None:
    async with db_session_factory() as session:
        return await get_thread_model(
            session,
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(guild_id)),
            platform="discord",
            thread_id=str(_THREAD_ID),
        )


async def test_a_member_switches_the_thread_and_sees_it_on_the_next_look(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 720000001
    await _registered_guild(db_session_factory, guild_id)
    runtime = _runtime(db_session_factory)

    assert await _run(_interaction(runtime, guild_id=guild_id), None) == (
        "This conversation runs on the agent's own model."
    )
    assert await _run(_interaction(runtime, guild_id=guild_id), "haiku") == (
        "This conversation runs on Haiku 5.5 from the next reply."
    )
    assert await _stored(db_session_factory, guild_id) == "claude-haiku-5-5"
    assert await _run(_interaction(runtime, guild_id=guild_id), None) == (
        "This conversation runs on Haiku 5.5."
    )


async def test_someone_who_may_not_start_a_turn_there_changes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 720000002
    await _registered_guild(
        db_session_factory, guild_id, TenantAccessPolicy(protected_channel_ids=(str(_PARENT_ID),))
    )
    runtime = _runtime(db_session_factory)

    content = await _run(_interaction(runtime, guild_id=guild_id), "opus")

    assert "Only people who can start a conversation here can change its model." in content
    assert await _stored(db_session_factory, guild_id) is None


async def test_outside_a_thread_it_points_at_one(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 720000003
    await _registered_guild(db_session_factory, guild_id)
    runtime = _runtime(db_session_factory)

    content = await _run(_interaction(runtime, guild_id=guild_id, in_thread=False), "opus")

    assert "Use /model inside a conversation thread." in content
    assert await _stored(db_session_factory, guild_id) is None
