"""Show GitHub removal notices in an admin's setup interaction."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.adapters.discord.agent_setup.github_connect_card import connect_embed
from daimon.adapters.discord.agent_setup.github_home import connect_button_view
from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.github_connect_cards import resolve_connect_card
from daimon.core.github_panel import connect_link, sync_connect_admin
from daimon.core.stores.github_removal_notices import claim_notice, finish_notice

import discord


async def send_pending_notice(
    runtime: DiscordRuntime, interaction: discord.Interaction, *, tenant_id: uuid.UUID
) -> None:
    """Deliver one queued removal notice when an admin opens setup."""
    if not is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]
        return
    async with runtime.sessionmaker.begin() as session:
        notice = await claim_notice(session, tenant_id=tenant_id, now=datetime.now(UTC))
    if notice is None:
        return
    try:
        async with runtime.sessionmaker.begin() as session:
            await sync_connect_admin(
                session,
                tenant_id=tenant_id,
                platform="discord",
                platform_user_id=str(interaction.user.id),
                verified_tenant_admin=True,
            )
            url = await connect_link(
                session,
                settings=runtime.settings,
                tenant_id=tenant_id,
                platform="discord",
                platform_user_id=str(interaction.user.id),
                verified_tenant_admin=True,
                requester_label=interaction.user.display_name,
                origin_parent_channel_id=str(interaction.channel_id),
                origin_thread_id=str(interaction.channel_id),
                origin_followup_token=f"{interaction.application_id}:{interaction.token}",
                origin_followup_expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
    except ValueError:
        async with runtime.sessionmaker.begin() as session:
            await finish_notice(session, notice=notice, delivered=False, now=datetime.now(UTC))
        return
    try:
        card = await resolve_connect_card(
            runtime.sessionmaker,
            runtime.settings,
            tenant_id=tenant_id,
            platform="discord",
            workspace_id=str(interaction.guild_id),
            agent_name=None,
        )
        await interaction.followup.send(
            f"GitHub connection to {notice.account_login} was removed.",
            embed=connect_embed(card),
            view=connect_button_view(url),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        async with runtime.sessionmaker.begin() as session:
            await finish_notice(session, notice=notice, delivered=False, now=datetime.now(UTC))
        return
    async with runtime.sessionmaker.begin() as session:
        await finish_notice(session, notice=notice, delivered=True, now=datetime.now(UTC))
