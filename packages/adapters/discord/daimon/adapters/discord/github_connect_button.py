"""Requester-bound GitHub link reveal for a shared conversation thread."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Self, cast

import structlog
from daimon.adapters.discord.agent_setup.github_home import connect_button_view
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.checks import is_member_guild_admin
from daimon.core.github_connect_cards import CONNECT_GITHUB_EMOJI
from daimon.core.github_credentials import build_multifernet, decrypt_token, encrypt_token
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.github_connect import bind_discord_connect_click

import discord
from discord.ext import commands

_log = structlog.get_logger(__name__)
CUSTOM_ID_TEMPLATE = r"gh_connect:(?P<requester>[0-9]+):(?P<intent>[0-9a-f]{32})"


class GitHubConnectButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=CUSTOM_ID_TEMPLATE
):
    """Reveal one invitation only to its requester, even after a bot restart."""

    def __init__(self, *, requester_id: str, intent_id: uuid.UUID) -> None:
        button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            label="Connect GitHub",
            emoji=CONNECT_GITHUB_EMOJI,
            style=discord.ButtonStyle.primary,
            custom_id=f"gh_connect:{requester_id}:{intent_id.hex}",
        )
        super().__init__(button)
        self.requester_id = requester_id
        self.intent_id = intent_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(requester_id=match["requester"], intent_id=uuid.UUID(hex=match["intent"]))

    async def callback(  # type: ignore[override]
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        try:
            await self._reveal(interaction)
        except Exception as error:
            _log.exception("github_connect.button_failed", error_type=type(error).__name__)
            if interaction.response.is_done():
                await interaction.followup.send(
                    "GitHub setup is unavailable. Try again.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "GitHub setup is unavailable. Try again.", ephemeral=True
                )

    async def _reveal(self, interaction: discord.Interaction[commands.Bot]) -> None:
        if str(interaction.user.id) != self.requester_id:
            await interaction.response.send_message(
                f"Only <@{self.requester_id}> can use this.", ephemeral=True
            )
            return
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "This connection is no longer available.", ephemeral=True
            )
            return
        guild = interaction.guild or interaction.client.get_guild(interaction.guild_id)
        if guild is None:
            await interaction.response.send_message(
                "This connection is no longer available.", ephemeral=True
            )
            return
        try:
            member = await guild.fetch_member(interaction.user.id)
        except discord.HTTPException:
            await interaction.response.send_message(
                "This connection is no longer available.", ephemeral=True
            )
            return
        if not is_member_guild_admin(member, guild_owner_id=guild.owner_id):
            await interaction.response.send_message(
                "Only a server admin can connect GitHub.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = cast(DaimonBot, interaction.client)
        settings = bot.runtime.settings
        root = settings.mcp.app_root_url
        if root is None:
            await interaction.followup.send("GitHub setup is unavailable.", ephemeral=True)
            return
        credentials = build_multifernet(
            tuple(key.get_secret_value() for key in settings.crypto.keys)
        )
        async with bot.runtime.sessionmaker.begin() as session:
            link = await bind_discord_connect_click(
                session,
                intent_id=self.intent_id,
                tenant_id=derive_tenant_uuid(
                    platform="discord", workspace_id=str(interaction.guild_id)
                ),
                requester_platform_user_id=self.requester_id,
                thread_id=str(interaction.channel_id),
                fernet=credentials,
                encrypted_followup=encrypt_token(
                    credentials, f"{interaction.application_id}:{interaction.token}"
                ),
                followup_expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
        if link is None:
            await interaction.followup.send(
                "This connection is no longer available.", ephemeral=True
            )
            return
        encrypted_token, agent_name = link
        token = decrypt_token(credentials, encrypted_token)
        await interaction.followup.send(
            f"Connect GitHub for {agent_name}." if agent_name else "Connect GitHub.",
            view=connect_button_view(f"{root}/oauth/github/connect/{token}"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
