"""Private GitHub entry screen in the Discord setup panel."""

from __future__ import annotations

from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.roster_view import RosterView
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.github_panel import CONNECT_COPY, connect_link
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.github_access import list_authorized_repos

import discord


class GitHubLinkView(discord.ui.View):
    """A private browser link with a copyable raw URL for forwarding."""

    def __init__(self, url: str, *, user_id: int) -> None:
        super().__init__(timeout=600)
        self.url = url
        self.user_id = user_id
        self.add_item(discord.ui.Button(label="Open GitHub ↗", url=url))

    @discord.ui.button(label="Copy link", style=discord.ButtonStyle.secondary)
    async def copy_link(
        self, interaction: discord.Interaction, button: discord.ui.Button[GitHubLinkView]
    ) -> None:
        del button
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This link is private.", ephemeral=True)
            return
        await interaction.response.send_message(
            self.url, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


class GitHubHomeView(PanelViewBase):
    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        connected_count: int,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        summary = (
            f"Connected: {connected_count} repos. Choose which agents can use them."
            if connected_count
            else "Connect repos here, then choose which agents can use them."
        )
        container.add_item(discord.ui.TextDisplay(f"## GitHub\n{summary}"))
        actions: discord.ui.ActionRow[GitHubHomeView] = discord.ui.ActionRow()
        connect: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
            label="Connect more repos" if connected_count else "Connect GitHub",
            style=discord.ButtonStyle.primary,
        )
        connect.callback = self._on_connect  # type: ignore[method-assign]
        if connected_count:
            choose: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
                label="Choose agent", style=discord.ButtonStyle.primary
            )
            choose.callback = self._on_choose  # type: ignore[method-assign]
            actions.add_item(choose)
            connect.style = discord.ButtonStyle.secondary
        actions.add_item(connect)
        back: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
            label="◀ Back", style=discord.ButtonStyle.secondary
        )
        back.callback = self._on_choose  # type: ignore[method-assign]
        actions.add_item(back)
        container.add_item(actions)
        self.add_item(container)

    async def _on_connect(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.state.guild_id or not is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]
            await interaction.response.send_message(
                "Only a server admin can connect GitHub.", ephemeral=True
            )
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with self.runtime.sessionmaker.begin() as session:
                url = await connect_link(
                    session,
                    settings=self.runtime.settings,
                    tenant_id=tenant_id,
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                )
        except ValueError:
            await interaction.followup.send(
                "GitHub didn't answer. Try again in a minute.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"{CONNECT_COPY}\nLink works once · expires in 7 days",
            view=GitHubLinkView(url, user_id=interaction.user.id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _on_choose(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.state.guild_id or not is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]
            await interaction.response.send_message(
                "Only a server admin can choose agents here.", ephemeral=True
            )
            return
        await self.swap_to(
            interaction,
            RosterView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
            ),
        )


async def load_home(state: PanelState, *, runtime: DiscordRuntime, user_id: int) -> GitHubHomeView:
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id))
    async with runtime.sessionmaker() as session:
        connected_count = sum(
            repo.status == "active"
            for repo in await list_authorized_repos(session, tenant_id=tenant_id)
        )
    return GitHubHomeView(
        state, runtime=runtime, allowed_user_id=user_id, connected_count=connected_count
    )
