"""GitHub link reveal on a Connect GitHub card in a shared conversation thread."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Self, cast

import structlog
from daimon.adapters.discord.agent_setup.github_connect_card import connect_embed
from daimon.adapters.discord.agent_setup.github_home import connect_button_view
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.checks import admin_caller, is_member_guild_admin
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.github_connect_cards import (
    CONNECT_GITHUB_EMOJI,
    ask_manager_line,
    resolve_connect_card,
)
from daimon.core.github_credentials import build_multifernet, decrypt_token, encrypt_token
from daimon.core.github_panel import can_manage_agent_github
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.stores.github_connect import bind_discord_connect_click, connect_intent_agent
from daimon.core.stores.security_audit import append_event

import discord
from discord.ext import commands

_log = structlog.get_logger(__name__)
CUSTOM_ID_TEMPLATE = r"gh_connect:(?P<requester>[0-9]+):(?P<intent>[0-9a-f]{32})"


class GitHubConnectButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=CUSTOM_ID_TEMPLATE
):
    """Reveal the card's invitation privately to its clicker, even after a bot restart.

    Only the person who asked, a server admin or an owner of the agent is shown the
    link; anyone else is told whom to ask. The invitation stays the requester's, who
    must still manage the agent, and is checked again when the repos are saved. The
    link adds repos to the agent; it never links the clicker's GitHub account to the
    requester's Daimon account.
    """

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
                    embed=build_notice_embed("GitHub setup is unavailable. Try again."),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    embed=build_notice_embed("GitHub setup is unavailable. Try again."),
                    ephemeral=True,
                )

    async def _reveal(self, interaction: discord.Interaction[commands.Bot]) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                embed=build_notice_embed("This connection is no longer available."), ephemeral=True
            )
            return
        guild = interaction.guild or interaction.client.get_guild(interaction.guild_id)
        if guild is None:
            await interaction.response.send_message(
                embed=build_notice_embed("This connection is no longer available."), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = cast(DaimonBot, interaction.client)
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
        clicker = await _clicker_check(
            bot,
            interaction.user,
            guild_owner_id=guild.owner_id,
            requester_id=self.requester_id,
            tenant_id=tenant_id,
            intent_id=self.intent_id,
        )
        if clicker.refusal is not None:
            await interaction.followup.send(
                embed=build_notice_embed(clicker.refusal),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            # The invitation is the requester's, so they must still manage the agent.
            requester = await guild.fetch_member(int(self.requester_id))
        except discord.HTTPException:
            await interaction.followup.send(
                embed=build_notice_embed("This connection is no longer available."), ephemeral=True
            )
            return
        manages = False
        if not is_member_guild_admin(requester, guild_owner_id=guild.owner_id):
            refusal = await _manager_refusal(bot, requester, tenant_id, self.intent_id)
            if refusal is not None:
                await interaction.followup.send(embed=build_notice_embed(refusal), ephemeral=True)
                return
            manages = True
        settings = bot.runtime.settings
        root = settings.mcp.app_root_url
        if root is None:
            await interaction.followup.send(
                embed=build_notice_embed("GitHub setup is unavailable."), ephemeral=True
            )
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
                requester_manages_agent=manages,
            )
            if link is not None and str(interaction.user.id) != self.requester_id:
                # Someone other than the requester was shown the requester's link.
                await append_event(
                    session,
                    tenant_id=tenant_id,
                    account_id=None,
                    agent_id=clicker.agent_id,
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                    tool_name="github_connect",
                    operation="github_connect",
                    outcome="allowed",
                    reason="connect link shown to another person",
                )
        if link is None:
            await interaction.followup.send(
                embed=build_notice_embed("This connection is no longer available."), ephemeral=True
            )
            return
        encrypted_token, agent_name = link
        token = decrypt_token(credentials, encrypted_token)
        card = await resolve_connect_card(
            bot.runtime.sessionmaker,
            settings,
            tenant_id=derive_tenant_uuid(
                platform="discord", workspace_id=str(interaction.guild_id)
            ),
            platform="discord",
            workspace_id=str(interaction.guild_id),
            agent_name=agent_name,
        )
        await interaction.followup.send(
            embed=connect_embed(card),
            view=connect_button_view(f"{root}/oauth/github/connect/{token}"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def _manager_refusal(
    bot: DaimonBot, member: discord.Member, tenant_id: uuid.UUID, intent_id: uuid.UUID
) -> str | None:
    """Why a requester who is not a server admin may not add repos, or None if they manage it."""
    async with bot.runtime.sessionmaker() as session:
        target = await connect_intent_agent(session, intent_id=intent_id)
    if target is None:
        return "This connection is no longer available."
    agent_name, ma_agent_id = target
    if ma_agent_id is None:
        return ask_manager_line(agent_name)
    live = await find_agent_by_derived_uuid(
        bot.runtime.anthropic,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id),
    )
    if live is None:
        return "This connection is no longer available."
    async with bot.runtime.sessionmaker() as session:
        manages = await can_manage_agent_github(
            session,
            tenant_id=tenant_id,
            platform="discord",
            caller=admin_caller(member),
            agent_names=(agent_name, live.name),
            ma_agent_id=ma_agent_id,
            is_daimon_managed=live.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
            default=bot.runtime.deployment_default,
        )
    return None if manages else ask_manager_line(agent_name)


@dataclass(frozen=True)
class _ClickerCheck:
    refusal: str | None
    agent_id: uuid.UUID | None = None


async def _clicker_check(
    bot: DaimonBot,
    user: discord.User | discord.Member,
    *,
    guild_owner_id: int | None,
    requester_id: str,
    tenant_id: uuid.UUID,
    intent_id: uuid.UUID,
) -> _ClickerCheck:
    """Whether this clicker may be shown the requester's link, read now.

    The requester always may. Anyone else must be a live server admin or an owner of
    the agent the card is bound to.
    """
    async with bot.runtime.sessionmaker() as session:
        target = await connect_intent_agent(session, intent_id=intent_id)
    if target is None:
        return _ClickerCheck("This connection is no longer available.")
    agent_name, ma_agent_id = target
    agent_id = (
        derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
        if ma_agent_id is not None
        else None
    )
    if str(user.id) == requester_id:
        return _ClickerCheck(None, agent_id)
    if isinstance(user, discord.Member) and is_member_guild_admin(
        user, guild_owner_id=guild_owner_id
    ):
        return _ClickerCheck(None, agent_id)
    if ma_agent_id is not None and agent_id is not None:
        live = await find_agent_by_derived_uuid(
            bot.runtime.anthropic, tenant_id=tenant_id, agent_id=agent_id
        )
        if live is not None:
            async with bot.runtime.sessionmaker() as session:
                owner = await can_manage_agent_github(
                    session,
                    tenant_id=tenant_id,
                    platform="discord",
                    caller=admin_caller(user),
                    agent_names=(agent_name, live.name),
                    ma_agent_id=ma_agent_id,
                    is_daimon_managed=live.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
                    default=bot.runtime.deployment_default,
                )
            if owner:
                return _ClickerCheck(None, agent_id)
    return _ClickerCheck(f"Ask <@{requester_id}> or an owner of {agent_name}.")
