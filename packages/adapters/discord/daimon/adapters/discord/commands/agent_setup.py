"""`/agent-setup` slash command — per-user roster panel."""

from __future__ import annotations

from typing import cast

import anthropic
import structlog
from daimon.adapters.discord.agent_setup.panel import (
    AgentSetupView,
    load_repo_binding,
    load_secret_count,
)
from daimon.adapters.discord.agent_setup.scope_default import list_guild_propagations
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.agent_setup.write import (
    load_selected_github_login,
    load_tenant_roster,
)
from daimon.adapters.discord.checks import (
    is_guild_admin,
    require_registered_guild,
    resolve_tenant_for_interaction,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.errors import DaimonError
from daimon.core.stores.identity import get_or_create_platform_principal

import discord
from discord import Interaction, app_commands
from discord.ext import commands

log = structlog.get_logger()
BotInteraction = Interaction[commands.Bot]


def _get_runtime(interaction: BotInteraction) -> DiscordRuntime:
    return cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]  # DaimonBot.runtime not on Bot type


@app_commands.guild_only()
class AgentSetupCog(commands.Cog):
    """Guild-shared agent management — single ephemeral panel."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot: commands.Bot = bot

    @app_commands.command(name="agent-setup", description="Manage this server's agents")
    @require_registered_guild
    async def agent_setup(self, interaction: BotInteraction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        rid = generate_request_id()
        try:
            runtime = _get_runtime(interaction)
            assert interaction.guild_id is not None, "require_registered_guild guarantees guild"
            assert interaction.channel_id is not None, "require_registered_guild guarantees channel"
            guild_id = interaction.guild_id
            channel_id = interaction.channel_id
            channel = interaction.channel
            channel_name = (
                channel.name
                if isinstance(channel, discord.abc.GuildChannel | discord.Thread)
                else None
            )
            is_admin = is_guild_admin(interaction)
            tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
            assert tenant_id is not None, "require_registered_guild guarantees a tenant"
            async with runtime.sessionmaker() as session:
                principal = await get_or_create_platform_principal(
                    session,
                    tenant_id=tenant_id,
                    platform="discord",
                    external_id=str(interaction.user.id),
                )
                await session.commit()
            roster = await load_tenant_roster(
                runtime.anthropic,
                tenant_id=tenant_id,
            )
            default_mcp_url = (
                str(runtime.settings.mcp.public_url)
                if runtime.settings.mcp.public_url is not None
                else None
            )
            async with runtime.sessionmaker() as session:
                cascade = await list_guild_propagations(session, tenant_id=tenant_id)
            secret_count = (
                await load_secret_count(runtime, tenant_id=tenant_id, agent_name=roster[0].name)
                if roster
                else 0
            )
            state = PanelState.initial(
                roster=roster,
                # KEEP — personal principal for PAT/skill-sync/MCP/audit-actor
                account_id=principal.account_id,
                # SC-2: guild ownership stamp
                guild_account_id=derive_guild_account_uuid(tenant_id),
                platform_principal_id=principal.id,
                default_mcp_url=default_mcp_url,
                is_admin=is_admin,
                guild_id=guild_id,
                channel_id=channel_id,
                channel_name=channel_name,
                cascade_view=cascade,
                deployment_default=runtime.deployment_default,
                secret_count=secret_count,
            )
            state.github_login = await load_selected_github_login(
                runtime, tenant_id=tenant_id, entry=state.selected
            )
            state.hydrate_repo_binding(
                await load_repo_binding(runtime, tenant_id=tenant_id, entry=state.selected)
            )
            state.fallback_pat_configured = runtime.settings.github.fallback_pat is not None
            thumbnail_url: str | None = (
                interaction.client.user.display_avatar.url
                if interaction.client.user is not None
                else None
            )
            await interaction.followup.send(
                view=AgentSetupView(
                    state,
                    runtime=runtime,
                    allowed_user_id=interaction.user.id,
                    thumbnail_url=thumbnail_url,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=True,
            )
        except (DaimonError, anthropic.APIError, discord.HTTPException) as exc:
            await interaction.followup.send(render_error(exc, request_id=rid), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AgentSetupCog(bot))
