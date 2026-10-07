"""Private new-repository cards for Discord server admins."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.github_panel import CONNECT_COPY, connect_link
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_connect import admin_account_for_platform_user
from daimon.core.stores.github_new_repo_notices import (
    NewRepoNotice,
    claim_next_notice,
    dismiss_notice,
    finish_notice,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.security_audit import append_event

import discord


class NewRepoCard(discord.ui.View):
    def __init__(self, runtime: DiscordRuntime, notice: NewRepoNotice, user_id: int) -> None:
        super().__init__(timeout=600)
        self.runtime = runtime
        self.notice = notice
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.user.id != self.user_id
            or interaction.guild_id is None
            or derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
            != self.notice.tenant_id
            or not is_guild_admin(interaction)  # pyright: ignore[reportArgumentType]
        ):
            await interaction.response.send_message(
                "Only the server admin who received this card can use it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Connect", style=discord.ButtonStyle.primary)
    async def connect(
        self, interaction: discord.Interaction, button: discord.ui.Button[NewRepoCard]
    ) -> None:
        del button
        try:
            async with self.runtime.sessionmaker.begin() as session:
                url = await connect_link(
                    session,
                    settings=self.runtime.settings,
                    tenant_id=self.notice.tenant_id,
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                    preselected_repo_full_name=self.notice.repo_full_name,
                )
        except ValueError:
            await interaction.response.send_message(
                "GitHub connection is unavailable.", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            content=f"{CONNECT_COPY}\n{url}",
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.stop()

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary)
    async def dismiss(
        self, interaction: discord.Interaction, button: discord.ui.Button[NewRepoCard]
    ) -> None:
        del button
        try:
            async with self.runtime.sessionmaker.begin() as session:
                principal = await get_or_create_platform_principal(
                    session,
                    tenant_id=self.notice.tenant_id,
                    platform="discord",
                    external_id=str(interaction.user.id),
                )
                await set_role(session, principal.account_id, Role.ADMIN)
                account_id = await admin_account_for_platform_user(
                    session,
                    tenant_id=self.notice.tenant_id,
                    external_id=str(interaction.user.id),
                )
                await dismiss_notice(
                    session,
                    tenant_id=self.notice.tenant_id,
                    installation_id=self.notice.installation_id,
                    repo_full_name=self.notice.repo_full_name,
                    now=datetime.now(UTC),
                )
                await append_event(
                    session,
                    tenant_id=self.notice.tenant_id,
                    account_id=account_id,
                    agent_id=None,
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                    tool_name="github_connect",
                    operation="github_connect",
                    outcome="allowed",
                    reason="new repo notice dismissed",
                )
        except ValueError:
            await interaction.response.send_message(
                "Only a server admin can dismiss this card.", ephemeral=True
            )
            return
        await interaction.response.edit_message(content="Dismissed.", view=None)
        self.stop()


async def send_pending_notice(
    runtime: DiscordRuntime,
    interaction: discord.Interaction,
    *,
    tenant_id: uuid.UUID,
) -> None:
    """Try one queued card after an admin opens the setup panel."""
    if not is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]
        return
    async with runtime.sessionmaker.begin() as session:
        notice = await claim_next_notice(session, tenant_id=tenant_id, now=datetime.now(UTC))
    if notice is None:
        return
    try:
        await interaction.followup.send(
            f"New repo `{notice.repo_full_name}` in the GitHub installation — connect it?",
            view=NewRepoCard(runtime, notice, interaction.user.id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        async with runtime.sessionmaker.begin() as session:
            await finish_notice(session, notice=notice, delivered=False, now=datetime.now(UTC))
        return
    async with runtime.sessionmaker.begin() as session:
        await finish_notice(session, notice=notice, delivered=True, now=datetime.now(UTC))
