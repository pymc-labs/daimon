"""`/agent-setup` slash command — per-user roster panel."""

from __future__ import annotations

from typing import cast

import anthropic
import structlog
from daimon.adapters.discord.agent_setup.avatar import upload_agent_avatar
from daimon.adapters.discord.agent_setup.hydrate import load_roster_state
from daimon.adapters.discord.agent_setup.roster_view import RosterView
from daimon.adapters.discord.checks import (
    is_guild_admin,
    require_registered_guild,
    resolve_tenant_for_interaction,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.errors import DaimonError
from daimon.core.stores.tenants import get_tenant

import discord
from discord import Interaction, app_commands
from discord.ext import commands

log = structlog.get_logger()
BotInteraction = Interaction[commands.Bot]


def _not_ready_message(provision_status: str) -> str:
    """Pure: copy for a tenant that isn't ready for the roster panel yet.

    An unrecognized status fails closed to the same message as "pending"
    rather than falling through to the panel.
    """
    if provision_status == "failed":
        return "Setup hit a snag — check the message I posted in this server, then try again."
    return "This install is still being set up — try again in a moment."


def _get_runtime(interaction: BotInteraction) -> DiscordRuntime:
    return cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]  # DaimonBot.runtime not on Bot type


@app_commands.guild_only()
class AgentSetupCog(commands.Cog):
    """Read-only orientation for this server's agents — one ephemeral panel."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot: commands.Bot = bot

    @app_commands.command(
        name="agent-setup",
        description="See your agents, who answers where, and make changes",
    )
    @app_commands.describe(
        agent="Agent whose picture to change (fallback: use with avatar)",
        avatar="Picture file, up to 2 MB (fallback: use with agent)",
    )
    @require_registered_guild
    async def agent_setup(
        self,
        interaction: BotInteraction,
        agent: str | None = None,
        avatar: discord.Attachment | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        rid = generate_request_id()
        try:
            runtime = _get_runtime(interaction)
            assert interaction.guild_id is not None, "require_registered_guild guarantees guild"
            is_admin = is_guild_admin(interaction)
            tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
            assert tenant_id is not None, "require_registered_guild guarantees a tenant"
            async with runtime.sessionmaker() as session:
                tenant_row = await get_tenant(session, tenant_id)
            assert tenant_row is not None, "require_registered_guild guarantees a tenant row"
            if tenant_row.provision_status != "ready":
                await interaction.edit_original_response(
                    content=_not_ready_message(tenant_row.provision_status),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if agent is not None or avatar is not None:
                if not agent or avatar is None:
                    await interaction.edit_original_response(
                        content="Choose an agent and picture file.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                message, _ = await upload_agent_avatar(
                    interaction,
                    runtime,
                    tenant_id=tenant_id,
                    agent_name=agent,
                    attachment=avatar,
                )
                await interaction.edit_original_response(
                    content=message,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            state = await load_roster_state(
                runtime, interaction, tenant_id=tenant_id, is_admin=is_admin
            )
            await interaction.edit_original_response(
                view=RosterView(
                    state,
                    runtime=runtime,
                    allowed_user_id=interaction.user.id,
                ).bind_render_interaction(interaction, panel=state),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, anthropic.APIError, discord.HTTPException) as exc:
            await interaction.followup.send(render_error(exc, request_id=rid), ephemeral=True)
