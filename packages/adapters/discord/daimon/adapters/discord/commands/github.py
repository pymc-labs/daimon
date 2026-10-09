"""Private /github connect link for one selected agent."""

from datetime import UTC, datetime, timedelta
from typing import cast

import anthropic
from daimon.adapters.discord.agent_setup.github_home import connect_button_view, load_home
from daimon.adapters.discord.agent_setup.hydrate import load_roster_state
from daimon.adapters.discord.checks import (
    is_guild_admin,
    require_registered_guild,
    resolve_tenant_for_interaction,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_connect import (
    CLIENT_AGENT_MESSAGE,
    ClientAgentConnectionError,
    mint_invitation,
    pending_update_for_agent,
    require_app_eligible_agent,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.security_audit import append_event

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]


@app_commands.guild_only()
class GitHubCog(commands.GroupCog, group_name="github", group_description="GitHub setup"):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: BotInteraction) -> None:
        from daimon.adapters.discord.agent_setup.github_new_repo import handle_dm_notice
        from daimon.adapters.discord.agent_setup.github_requests import handle_request_card

        runtime = cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]
        if not await handle_dm_notice(interaction, runtime):
            await handle_request_card(interaction, runtime)

    @app_commands.command(name="home", description="GitHub repos and account status")
    @require_registered_guild
    async def home(self, interaction: BotInteraction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("GitHub is unavailable here.", ephemeral=True)
            return
        runtime = cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]
        await interaction.response.defer(ephemeral=True, thinking=True)
        tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
        if tenant_id is None:
            await interaction.followup.send("GitHub is unavailable here.", ephemeral=True)
            return
        state = await load_roster_state(
            runtime,
            interaction,
            tenant_id=tenant_id,
            is_admin=is_guild_admin(interaction),  # pyright: ignore[reportArgumentType]
        )
        home = await load_home(state, runtime=runtime, user_id=interaction.user.id)
        message = await interaction.edit_original_response(
            embed=home.embed,
            view=home.bind_render_interaction(interaction, panel=state),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        home.attach_message(message)
        if is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]
            from daimon.adapters.discord.agent_setup.github_new_repo import (
                send_pending_notice as send_pending_new_repo_notice,
            )
            from daimon.adapters.discord.agent_setup.github_removal import (
                send_pending_notice as send_pending_removal_notice,
            )

            await send_pending_new_repo_notice(runtime, interaction, tenant_id=tenant_id)
            await send_pending_removal_notice(runtime, interaction, tenant_id=tenant_id)

    @app_commands.command(name="connect", description="Connect repos to one agent")
    @app_commands.describe(agent="Agent to connect")
    @require_registered_guild
    async def connect(self, interaction: BotInteraction, agent: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_guild_admin(interaction):
            await interaction.followup.send("Ask an admin", ephemeral=True)
            return
        runtime = cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]
        root = runtime.settings.mcp.app_root_url
        config = runtime.settings.github_app
        if root is None or not all(
            (
                config.app_id,
                config.app_slug,
                config.private_key,
                config.client_id,
                config.client_secret,
            )
        ):
            await interaction.followup.send("GitHub setup is unavailable.", ephemeral=True)
            return
        tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
        if tenant_id is None:
            await interaction.followup.send("This server is not set up yet.", ephemeral=True)
            return
        try:
            if agent is None:
                state = await load_roster_state(
                    runtime, interaction, tenant_id=tenant_id, is_admin=True
                )
                selected = state.selected_agent
                if selected is None:
                    await interaction.followup.send(
                        "Choose an agent with /github connect.", ephemeral=True
                    )
                    return
                target_name, target_ma_id = selected.name, selected.ma_agent_id
            else:
                agents = await list_agents_by_tenant(runtime.anthropic, tenant_id=tenant_id)
                matches = [row for row in agents if row.name.casefold() == agent.casefold()]
                if len(matches) != 1:
                    await interaction.followup.send("Choose one current agent.", ephemeral=True)
                    return
                target_name, target_ma_id = matches[0].name, str(matches[0].id)
            agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=target_ma_id)
            async with runtime.sessionmaker() as session:
                pending = await pending_update_for_agent(
                    session, tenant_id=tenant_id, agent_id=agent_id
                )
            if pending is not None:
                await interaction.followup.send(CLIENT_AGENT_MESSAGE, ephemeral=True)
                return
            async with runtime.sessionmaker() as session:
                await require_app_eligible_agent(
                    session, tenant_id=tenant_id, agent_id=agent_id, agent_name=target_name
                )
            async with runtime.sessionmaker.begin() as session:
                principal = await get_or_create_platform_principal(
                    session,
                    tenant_id=tenant_id,
                    platform="discord",
                    external_id=str(interaction.user.id),
                )
                await set_role(session, principal.account_id, Role.ADMIN)
                token = await mint_invitation(
                    session,
                    tenant_id=tenant_id,
                    requester_account_id=principal.account_id,
                    requester_label=str(interaction.user.id),
                    requester_platform_user_id=str(interaction.user.id),
                    agent_id=agent_id,
                    agent_name=target_name,
                    origin_platform="discord",
                    origin_parent_channel_id=str(interaction.channel_id),
                    origin_thread_id=str(interaction.channel_id),
                    encrypted_origin_followup=encrypt_token(
                        build_multifernet(
                            tuple(key.get_secret_value() for key in runtime.settings.crypto.keys)
                        ),
                        f"{interaction.application_id}:{interaction.token}",
                    ),
                    origin_followup_expires_at=datetime.now(UTC) + timedelta(minutes=15),
                )
                await append_event(
                    session,
                    tenant_id=tenant_id,
                    account_id=principal.account_id,
                    agent_id=agent_id,
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                    tool_name="github_connect",
                    operation="github_connect",
                    outcome="allowed",
                    reason="admin link minted",
                )
            await interaction.followup.send(
                f"Connect GitHub for {target_name}.",
                view=connect_button_view(f"{root}/oauth/github/connect/{token}"),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except ClientAgentConnectionError:
            await interaction.followup.send(CLIENT_AGENT_MESSAGE, ephemeral=True)
        except (anthropic.APIError, discord.HTTPException, ValueError):
            await interaction.followup.send(
                "GitHub setup is unavailable. Try again.", ephemeral=True
            )
