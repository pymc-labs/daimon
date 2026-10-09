"""Private GitHub entry screen in the Discord setup panel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.adapters.discord.agent_setup.github_card_ui import github_embed
from daimon.adapters.discord.agent_setup.github_embed_panel import (
    EmbedActionRow,
)
from daimon.adapters.discord.agent_setup.github_embed_panel import (
    GitHubEmbedPanel as PanelViewBase,
)
from daimon.adapters.discord.agent_setup.roster_view import RosterView
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.github_connect_cards import CONNECT_GITHUB_EMOJI
from daimon.core.github_panel import (
    CONNECT_COPY,
    connect_link,
    pending_connect_link,
    safe_github_error,
)
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_access_requests import list_asker_requests
from daimon.core.stores.github_connected_repos import summary as connected_summary
from daimon.core.stores.github_links import account_link_status
from daimon.core.stores.github_personal_links import mint_link
from daimon.core.stores.identity import get_or_create_platform_principal

import discord


def connect_button(url: str) -> discord.ui.Button[discord.ui.View]:
    """The shared Discord link button for private GitHub connection URLs."""
    return discord.ui.Button(
        label="Connect GitHub", emoji=CONNECT_GITHUB_EMOJI, style=discord.ButtonStyle.link, url=url
    )


def connect_button_view(url: str, *, timeout: float | None = None) -> discord.ui.View:
    view = discord.ui.View(timeout=timeout)
    view.add_item(connect_button(url))
    return view


class GitHubLinkView(discord.ui.View):
    """A private browser link displayed as a button."""

    def __init__(self, url: str) -> None:
        super().__init__(timeout=600)
        self.add_item(connect_button(url))


class GitHubHomeView(PanelViewBase):
    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        connected_count: int,
        owners: tuple[str, ...] = (),
        agent_count: int = 0,
        pending_url: str | None = None,
        linked_login: str | None = None,
        own_waiting_count: int = 0,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        self.pending_url = pending_url
        self.linked_login = linked_login
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        source = f" from {', '.join(owners)}" if owners else ""
        summary = (
            f"Connected: {connected_count} {'repo' if connected_count == 1 else 'repos'}{source}."
            if connected_count
            else "Connect repos here, then choose which agents can use them."
        )
        if not state.is_admin:
            summary = "Server admins connect repos here."
        status = f"Linked as @{linked_login}" if linked_login else "GitHub isn't linked."
        container.add_item(
            discord.ui.TextDisplay(
                "## GitHub\nConnection in progress."
                if pending_url and state.is_admin
                else f"## GitHub\n{summary}"
            )
        )
        if connected_count:
            container.add_item(discord.ui.TextDisplay(f"Agents\n{agent_count} using these repos"))
        container.add_item(discord.ui.TextDisplay(f"Personal link\n{status}"))
        actions: EmbedActionRow = EmbedActionRow()
        personal_actions: EmbedActionRow = EmbedActionRow()
        personal: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
            label="Use another account" if linked_login else "Link GitHub",
            style=discord.ButtonStyle.secondary,
        )
        personal.callback = self._on_personal_link  # type: ignore[method-assign]
        (personal_actions if state.is_admin else actions).add_item(personal)
        if linked_login:
            unlink: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
                label="Unlink", style=discord.ButtonStyle.secondary
            )
            unlink.callback = self._on_unlink_prompt  # type: ignore[method-assign]
            (personal_actions if state.is_admin else actions).add_item(unlink)
        if state.is_admin and pending_url:
            actions.add_item(connect_button(pending_url))
            start_over: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
                label="Start over", style=discord.ButtonStyle.secondary
            )
            start_over.callback = self._on_connect  # type: ignore[method-assign]
            actions.add_item(start_over)
        elif state.is_admin:
            connect: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
                label="Connect more repos" if connected_count else "Connect GitHub",
                emoji=CONNECT_GITHUB_EMOJI if not connected_count else None,
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
            if connected_count:
                manage: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
                    label="Manage connected repos", style=discord.ButtonStyle.secondary
                )
                manage.callback = self._on_manage  # type: ignore[method-assign]
                actions.add_item(manage)
        elif state.can_manage_github_agents:
            choose: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
                label="Choose agent", style=discord.ButtonStyle.primary
            )
            choose.callback = self._on_choose  # type: ignore[method-assign]
            actions.add_item(choose)
        back: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
            label="◀ Back", style=discord.ButtonStyle.secondary
        )
        back.callback = self._on_back  # type: ignore[method-assign]
        actions.add_item(back)
        container.add_item(actions)
        if state.is_admin:
            container.add_item(personal_actions)
        if state.can_manage_github_agents or own_waiting_count:
            waiting_row: EmbedActionRow = EmbedActionRow()
            waiting: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
                label="Requests waiting", style=discord.ButtonStyle.secondary
            )
            waiting.callback = self._on_waiting  # type: ignore[method-assign]
            waiting_row.add_item(waiting)
            container.add_item(waiting_row)
        self.add_item(container)

    async def _on_waiting(self, interaction: discord.Interaction) -> None:
        if (
            interaction.user.id != self.allowed_user_id
            or interaction.guild_id != self.state.guild_id
        ):
            await interaction.response.send_message("This panel is private.", ephemeral=True)
            return
        from daimon.adapters.discord.agent_setup.github_waiting import load_waiting_view

        view = await load_waiting_view(
            self.state, runtime=self.runtime, user_id=self.allowed_user_id, interaction=interaction
        )
        await self.swap_to(interaction, view)

    async def _on_connect(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.state.guild_id or not is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]
            await interaction.response.send_message(
                "Only a server admin can connect GitHub.", ephemeral=True
            )
            return
        agent = self.state.answering or (
            self.state.roster_agents[0] if len(self.state.roster_agents) == 1 else None
        )
        if agent is None and self.state.roster_agents:
            await self._on_choose(interaction)
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with self.runtime.sessionmaker.begin() as session:
                principal = await get_or_create_platform_principal(
                    session,
                    tenant_id=tenant_id,
                    platform="discord",
                    external_id=str(interaction.user.id),
                )
                await set_role(session, principal.account_id, Role.ADMIN)
                url = await connect_link(
                    session,
                    settings=self.runtime.settings,
                    tenant_id=tenant_id,
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                    verified_tenant_admin=True,
                    workspace_label=interaction.guild.name if interaction.guild else None,
                    requester_label=interaction.user.display_name,
                    start_over=self.pending_url is not None,
                    agent_id=(
                        derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.ma_agent_id)
                        if agent is not None
                        else None
                    ),
                    agent_name=agent.name if agent is not None else None,
                    origin_parent_channel_id=str(interaction.channel_id),
                    origin_thread_id=str(interaction.channel_id),
                    origin_followup_token=f"{interaction.application_id}:{interaction.token}",
                    origin_followup_expires_at=datetime.now(UTC) + timedelta(minutes=15),
                )
        except ValueError as error:
            await interaction.followup.send(safe_github_error(error), ephemeral=True)
            return
        await interaction.followup.send(
            embed=github_embed(CONNECT_COPY, state="waiting"),
            view=GitHubLinkView(url),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _on_choose(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.state.guild_id:
            await interaction.response.send_message(
                "You cannot change this agent's GitHub repos.", ephemeral=True
            )
            return
        from daimon.adapters.discord.checks import channel_admin_caller
        from daimon.core.channel_admins import load_administered_channel_ids

        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
        async with self.runtime.sessionmaker() as session:
            managed = await load_administered_channel_ids(
                session,
                tenant_id=tenant_id,
                platform="discord",
                caller=channel_admin_caller(interaction.user),
            )
        if not is_guild_admin(interaction) and not managed:  # pyright: ignore[reportArgumentType]
            await interaction.response.send_message(
                "You cannot change this agent's GitHub repos.", ephemeral=True
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

    async def _on_back(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild_id != self.state.guild_id
            or interaction.user.id != self.allowed_user_id
        ):
            await interaction.response.send_message("This panel is private.", ephemeral=True)
            return
        await self.swap_to(
            interaction,
            RosterView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
            ),
        )

    async def _on_personal_link(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild_id != self.state.guild_id
            or interaction.user.id != self.allowed_user_id
        ):
            await interaction.response.send_message("This panel is private.", ephemeral=True)
            return
        root = self.runtime.settings.mcp.app_root_url
        if root is None:
            await interaction.response.send_message(
                "GitHub didn't answer. Try again in a minute.", ephemeral=True
            )
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        try:
            async with self.runtime.sessionmaker.begin() as session:
                url = await mint_link(
                    session,
                    tenant_id=tenant_id,
                    account_id=self.state.account_id,
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                    root_url=str(root),
                )
        except ValueError:
            await interaction.response.send_message(
                "GitHub didn't answer. Try again in a minute.", ephemeral=True
            )
            return
        view = connect_button_view(url, timeout=600)
        await interaction.response.send_message(
            embed=github_embed("Link GitHub to your account."),
            view=view,
            ephemeral=True,
        )

    async def _on_unlink_prompt(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild_id != self.state.guild_id
            or interaction.user.id != self.allowed_user_id
        ):
            await interaction.response.send_message("This panel is private.", ephemeral=True)
            return
        await self.swap_to(
            interaction,
            GitHubUnlinkView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
            ),
        )

    async def _on_manage(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.state.guild_id or not is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]
            await interaction.response.send_message(
                "Only a server admin can manage connected repos.", ephemeral=True
            )
            return
        from daimon.adapters.discord.agent_setup.github_manage import GitHubManageView
        from daimon.core.stores.accounts import get_account
        from daimon.core.stores.github_access import list_authorized_repos

        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
        async with self.runtime.sessionmaker() as session:
            account = await get_account(session, self.state.account_id)
            if account is None or account.is_external or account.tenant_id != tenant_id:
                await interaction.response.send_message(
                    "Only a server admin can manage connected repos.", ephemeral=True
                )
                return
            repos = tuple(
                repo
                for repo in await list_authorized_repos(session, tenant_id=tenant_id)
                if repo.status == "active"
            )
        await self.swap_to(
            interaction,
            GitHubManageView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                repos=repos,
            ),
        )


class GitHubUnlinkView(PanelViewBase):
    def __init__(self, state: PanelState, *, runtime: DiscordRuntime, allowed_user_id: int) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("Unlink GitHub from your account?"))
        row: EmbedActionRow = EmbedActionRow()
        unlink: discord.ui.Button[GitHubUnlinkView] = discord.ui.Button(
            label="Unlink", style=discord.ButtonStyle.danger
        )
        unlink.callback = self._on_unlink  # type: ignore[method-assign]
        row.add_item(unlink)
        back: discord.ui.Button[GitHubUnlinkView] = discord.ui.Button(label="◀ Back")
        back.callback = self._on_back  # type: ignore[method-assign]
        row.add_item(back)
        container.add_item(row)
        self.add_item(container)

    async def _on_unlink(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild_id != self.state.guild_id
            or interaction.user.id != self.allowed_user_id
        ):
            await interaction.response.send_message("This panel is private.", ephemeral=True)
            return
        from daimon.core.stores.github_links import unlink_verified_identity

        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        async with self.runtime.sessionmaker.begin() as session:
            await unlink_verified_identity(
                session,
                tenant_id=tenant_id,
                platform="discord",
                platform_user_id=str(interaction.user.id),
                account_id=self.state.account_id,
            )
        await self._on_back(interaction)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        home = await load_home(self.state, runtime=self.runtime, user_id=self.allowed_user_id)
        await self.swap_to(interaction, home)


async def load_home(state: PanelState, *, runtime: DiscordRuntime, user_id: int) -> GitHubHomeView:
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id))
    async with runtime.sessionmaker() as session:
        connected = (
            await connected_summary(session, tenant_id=tenant_id) if state.is_admin else None
        )
        pending_url = (
            await pending_connect_link(
                session,
                settings=runtime.settings,
                tenant_id=tenant_id,
                requester_account_id=state.account_id,
            )
            if state.is_admin
            else None
        )
        linked_login = await account_link_status(session, account_id=state.account_id)
        own_waiting = await list_asker_requests(
            session, tenant_id=tenant_id, account_id=state.account_id
        )
    return GitHubHomeView(
        state,
        runtime=runtime,
        allowed_user_id=user_id,
        connected_count=connected.count if connected else 0,
        owners=connected.owners if connected else (),
        agent_count=connected.agent_count if connected else 0,
        pending_url=pending_url,
        linked_login=linked_login,
        own_waiting_count=len(own_waiting),
    )
