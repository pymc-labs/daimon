"""Private /github connect link for one selected agent."""

import uuid
from typing import cast

import anthropic
from daimon.adapters.discord.agent_setup.hydrate import load_roster_state
from daimon.adapters.discord.checks import (
    is_guild_admin,
    require_registered_guild,
    resolve_tenant_for_interaction,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_connect import (
    CLIENT_AGENT_MESSAGE,
    ClientAgentConnectionError,
    activate_pending_agent,
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


class PendingUpdateView(discord.ui.View):
    """One private, requester-bound confirmation for an agent's saved key."""

    def __init__(
        self,
        *,
        runtime: DiscordRuntime,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        allowed_user_id: int,
    ) -> None:
        super().__init__(timeout=900)
        self.runtime = runtime
        self.tenant_id = tenant_id
        self.agent_id = agent_id
        self.allowed_user_id = allowed_user_id

    @discord.ui.button(label="Update and restart chats", style=discord.ButtonStyle.primary)
    async def update(
        self, interaction: BotInteraction, button: discord.ui.Button["PendingUpdateView"]
    ) -> None:
        if interaction.user.id != self.allowed_user_id or not is_guild_admin(interaction):
            await interaction.response.send_message("Ask an admin", ephemeral=True)
            return
        try:
            async with self.runtime.sessionmaker.begin() as session:
                principal = await get_or_create_platform_principal(
                    session,
                    tenant_id=self.tenant_id,
                    platform="discord",
                    external_id=str(interaction.user.id),
                )
                await set_role(session, principal.account_id, Role.ADMIN)
                updated = await activate_pending_agent(
                    session,
                    tenant_id=self.tenant_id,
                    agent_id=self.agent_id,
                    account_id=principal.account_id,
                )
            await interaction.response.edit_message(
                content=(
                    "GitHub updated. Open chats restart on their next turn."
                    if updated
                    else "Already updated."
                ),
                view=None,
            )
        except ClientAgentConnectionError:
            await interaction.response.send_message(CLIENT_AGENT_MESSAGE, ephemeral=True)
        except ValueError:
            await interaction.response.send_message(
                "The update could not be completed. Check the agent's repos in GitHub setup.",
                ephemeral=True,
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: BotInteraction, button: discord.ui.Button["PendingUpdateView"]
    ) -> None:
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                "This card belongs to someone else.", ephemeral=True
            )
            return
        await interaction.response.edit_message(content="Update cancelled.", view=None)


@app_commands.guild_only()
class GitHubCog(commands.GroupCog, group_name="github", group_description="GitHub setup"):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

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
                await interaction.followup.send(
                    f"Update {target_name}? Its saved key is deleted and open chats restart. "
                    "Unsaved work in those chats is lost.",
                    view=PendingUpdateView(
                        runtime=runtime,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        allowed_user_id=interaction.user.id,
                    ),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
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
                    agent_id=agent_id,
                    agent_name=target_name,
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
                f"Opens GitHub to pick repos for {target_name}.\n"
                "Nothing is shared until you confirm.",
                view=discord.ui.View().add_item(
                    discord.ui.Button(
                        label="Connect GitHub",
                        style=discord.ButtonStyle.link,
                        url=f"{root}/oauth/github/connect/{token}",
                    )
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except ClientAgentConnectionError:
            await interaction.followup.send(CLIENT_AGENT_MESSAGE, ephemeral=True)
        except (anthropic.APIError, discord.HTTPException, ValueError):
            await interaction.followup.send(
                "GitHub setup is unavailable. Try again.", ephemeral=True
            )
