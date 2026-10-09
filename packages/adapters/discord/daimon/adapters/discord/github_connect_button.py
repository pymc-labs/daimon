"""Requester-bound GitHub link reveal for a shared conversation thread."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Self, cast

import structlog
from daimon.adapters.discord.agent_setup.github_home import connect_button_view
from daimon.adapters.discord.bot import DaimonBot
from daimon.core.github_credentials import build_multifernet, decrypt_token, encrypt_token
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.github_connect import bind_discord_connect_click

import discord
from discord.ext import commands

_log = structlog.get_logger(__name__)
CUSTOM_ID_TEMPLATE = r"gh_connect:(?P<requester>[0-9]+):(?P<token>[0-9a-f]{64})"


class GitHubConnectButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=CUSTOM_ID_TEMPLATE
):
    """Reveal one invitation only to its requester, even after a bot restart."""

    def __init__(self, *, requester_id: str, token_hash: str) -> None:
        button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            label="Connect GitHub",
            style=discord.ButtonStyle.primary,
            custom_id=f"gh_connect:{requester_id}:{token_hash}",
        )
        super().__init__(button)
        self.requester_id = requester_id
        self.token_hash = token_hash

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(requester_id=match["requester"], token_hash=match["token"])

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
                token_hash=self.token_hash,
                tenant_id=derive_tenant_uuid(
                    platform="discord", workspace_id=str(interaction.guild_id)
                ),
                requester_platform_user_id=self.requester_id,
                thread_id=str(interaction.channel_id),
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
