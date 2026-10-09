"""Private new-repository cards for Discord server admins."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from daimon.adapters.discord.agent_setup.github_card_ui import github_embed
from daimon.adapters.discord.agent_setup.github_home import GitHubLinkView, connect_button_view
from daimon.adapters.discord.checks import is_guild_admin, is_member_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.github_credentials import build_multifernet
from daimon.core.github_notice_visibility import new_repo_notice_copy, visible_new_repo_names
from daimon.core.github_panel import (
    connect_link,
    safe_github_error,
    sync_connect_admin,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.github_connect import admin_account_for_platform_user
from daimon.core.stores.github_new_repo_notices import (
    NewRepoNoticeGroup,
    claim_notice_group,
    dismiss_notice,
    finish_notice,
    notices_for_day,
)
from daimon.core.stores.security_audit import append_event
from daimon.core.stores.tenants import get_tenant

import discord


async def handle_dm_notice(interaction: discord.Interaction, runtime: DiscordRuntime) -> bool:
    custom_id = str((interaction.data or {}).get("custom_id") or "")
    if not custom_id.startswith("github_notice:"):
        return False
    parts = custom_id.split(":")
    try:
        if len(parts) != 4 or parts[3] not in ("connect", "dismiss"):
            raise ValueError("invalid notice")
        tenant_id = uuid.UUID(parts[1])
    except ValueError:
        await interaction.response.send_message("This card is unavailable.", ephemeral=True)
        return True
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with runtime.sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
        group = await notices_for_day(session, tenant_id=tenant_id, day=parts[2])
    if tenant is None or group is None:
        await interaction.followup.send("This card is unavailable.", ephemeral=True)
        return True
    guild = interaction.client.get_guild(int(tenant.external_id))
    if guild is None:
        await interaction.followup.send("This card is unavailable.", ephemeral=True)
        return True
    try:
        member = guild.get_member(interaction.user.id) or await guild.fetch_member(
            interaction.user.id
        )
    except discord.HTTPException:
        member = None
    if member is None or not is_member_guild_admin(member, guild_owner_id=guild.owner_id):
        await interaction.followup.send("Only a server admin can connect repos.", ephemeral=True)
        return True
    async with runtime.sessionmaker.begin() as session:
        await sync_connect_admin(
            session,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id=str(interaction.user.id),
            verified_tenant_admin=True,
        )
        account_id = await admin_account_for_platform_user(
            session, tenant_id=tenant_id, external_id=str(interaction.user.id)
        )
    visible: tuple[str, ...] = ()
    if runtime.settings.crypto.keys:
        fernet = build_multifernet(
            tuple(key.get_secret_value() for key in runtime.settings.crypto.keys)
        )
        async with runtime.sessionmaker() as session, httpx.AsyncClient(timeout=5) as client:
            visible = await visible_new_repo_names(
                session,
                group=group,
                account_id=account_id,
                platform="discord",
                platform_user_id=str(interaction.user.id),
                fernet=fernet,
                http_client=client,
            )
    if parts[3] == "dismiss":
        async with runtime.sessionmaker.begin() as session:
            for notice in group.notices:
                if notice.repo_full_name in visible:
                    await dismiss_notice(
                        session,
                        tenant_id=tenant_id,
                        installation_id=notice.installation_id,
                        repo_full_name=notice.repo_full_name,
                        now=datetime.now(UTC),
                    )
        await interaction.followup.send(
            "Connect later: /github home → Connect more repos.", ephemeral=True
        )
        return True
    try:
        async with runtime.sessionmaker.begin() as session:
            await sync_connect_admin(
                session,
                tenant_id=tenant_id,
                platform="discord",
                platform_user_id=str(interaction.user.id),
                verified_tenant_admin=is_member_guild_admin(member, guild_owner_id=guild.owner_id),
            )
            url = await connect_link(
                session,
                settings=runtime.settings,
                tenant_id=tenant_id,
                platform="discord",
                platform_user_id=str(interaction.user.id),
                verified_tenant_admin=is_member_guild_admin(member, guild_owner_id=guild.owner_id),
                workspace_label=guild.name,
                requester_label=member.display_name,
                origin_parent_channel_id=str(interaction.channel_id),
                origin_thread_id=str(interaction.channel_id),
                origin_followup_token=f"{interaction.application_id}:{interaction.token}",
                origin_followup_expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
    except ValueError as error:
        await interaction.followup.send(safe_github_error(error), ephemeral=True)
        return True
    from daimon.adapters.discord.agent_setup.github_connect_card import connect_embed
    from daimon.core.github_connect_cards import resolve_connect_card

    card = await resolve_connect_card(
        runtime.sessionmaker,
        runtime.settings,
        tenant_id=tenant_id,
        platform="discord",
        workspace_id=str(interaction.guild_id),
        agent_name=None,
    )
    view = connect_button_view(url)
    await interaction.followup.send(
        embed=connect_embed(card),
        view=view,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    return True


class NewRepoCard(discord.ui.View):
    def __init__(
        self,
        runtime: DiscordRuntime,
        group: NewRepoNoticeGroup,
        user_id: int,
        *,
        visible_names: tuple[str, ...] = (),
    ) -> None:
        super().__init__(timeout=600)
        self.runtime = runtime
        self.group = group
        self.user_id = user_id
        self.visible_names = visible_names
        copy = new_repo_notice_copy(visible_names)
        connect: discord.ui.Button[NewRepoCard] = discord.ui.Button(
            label=copy.connect_label, style=discord.ButtonStyle.primary
        )
        connect.callback = self.connect  # type: ignore[method-assign]
        self.add_item(connect)
        if copy.dismiss_label is not None:
            dismiss: discord.ui.Button[NewRepoCard] = discord.ui.Button(
                label=copy.dismiss_label, style=discord.ButtonStyle.secondary
            )
            dismiss.callback = self.dismiss  # type: ignore[method-assign]
            self.add_item(dismiss)
        back: discord.ui.Button[NewRepoCard] = discord.ui.Button(
            label="Back", style=discord.ButtonStyle.secondary
        )
        back.callback = self.back  # type: ignore[method-assign]
        self.add_item(back)

    async def back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="Back to GitHub setup.", embed=None, view=None
        )
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.user.id != self.user_id
            or interaction.guild_id is None
            or derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
            != self.group.tenant_id
            or not is_guild_admin(interaction)  # pyright: ignore[reportArgumentType]
        ):
            await interaction.response.send_message(
                "Only the server admin who received this card can use it.", ephemeral=True
            )
            return False
        return True

    async def connect(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False)
        try:
            async with self.runtime.sessionmaker.begin() as session:
                await sync_connect_admin(
                    session,
                    tenant_id=self.group.tenant_id,
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                    verified_tenant_admin=is_guild_admin(interaction),  # pyright: ignore[reportArgumentType]
                )
                url = await connect_link(
                    session,
                    settings=self.runtime.settings,
                    tenant_id=self.group.tenant_id,
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                    verified_tenant_admin=is_guild_admin(interaction),  # pyright: ignore[reportArgumentType]
                    workspace_label=interaction.guild.name if interaction.guild else None,
                    requester_label=interaction.user.display_name,
                    origin_parent_channel_id=str(interaction.channel_id),
                    origin_thread_id=str(interaction.channel_id),
                    origin_followup_token=f"{interaction.application_id}:{interaction.token}",
                    origin_followup_expires_at=datetime.now(UTC) + timedelta(minutes=15),
                )
        except ValueError:
            await interaction.followup.send("GitHub connection is unavailable.", ephemeral=True)
            return
        from daimon.adapters.discord.agent_setup.github_connect_card import connect_embed
        from daimon.core.github_connect_cards import resolve_connect_card

        card = await resolve_connect_card(
            self.runtime.sessionmaker,
            self.runtime.settings,
            tenant_id=self.group.tenant_id,
            platform="discord",
            workspace_id=str(interaction.guild_id),
            agent_name=None,
        )
        await interaction.edit_original_response(
            content=None,
            embed=connect_embed(card),
            view=GitHubLinkView(url),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.stop()

    async def dismiss(self, interaction: discord.Interaction) -> None:
        try:
            async with self.runtime.sessionmaker.begin() as session:
                account_id = await admin_account_for_platform_user(
                    session,
                    tenant_id=self.group.tenant_id,
                    external_id=str(interaction.user.id),
                )
                for notice in self.group.notices:
                    await dismiss_notice(
                        session,
                        tenant_id=notice.tenant_id,
                        installation_id=notice.installation_id,
                        repo_full_name=notice.repo_full_name,
                        now=datetime.now(UTC),
                    )
                await append_event(
                    session,
                    tenant_id=self.group.tenant_id,
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
        await interaction.response.edit_message(
            content=None,
            embed=github_embed("Connect later: /github home → Connect more repos."),
            view=None,
        )
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
        group = await claim_notice_group(session, tenant_id=tenant_id, now=datetime.now(UTC))
    if group is None:
        return
    visible_names: tuple[str, ...] = ()
    if runtime.settings.crypto.keys:
        async with runtime.sessionmaker() as session:
            try:
                account_id = await admin_account_for_platform_user(
                    session,
                    tenant_id=tenant_id,
                    external_id=str(interaction.user.id),
                )
            except ValueError:
                account_id = None
            if account_id is not None:
                fernet = build_multifernet(
                    tuple(key.get_secret_value() for key in runtime.settings.crypto.keys)
                )
                async with httpx.AsyncClient(timeout=5) as client:
                    visible_names = await visible_new_repo_names(
                        session,
                        group=group,
                        account_id=account_id,
                        platform="discord",
                        platform_user_id=str(interaction.user.id),
                        fernet=fernet,
                        http_client=client,
                    )
    copy = new_repo_notice_copy(visible_names)
    try:
        await interaction.followup.send(
            copy.text,
            view=NewRepoCard(runtime, group, interaction.user.id, visible_names=visible_names),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        async with runtime.sessionmaker.begin() as session:
            for notice in group.notices:
                await finish_notice(session, notice=notice, delivered=False, now=datetime.now(UTC))
        return
    async with runtime.sessionmaker.begin() as session:
        for notice in group.notices:
            await finish_notice(session, notice=notice, delivered=True, now=datetime.now(UTC))
