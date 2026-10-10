"""Private GitHub entry screen in the Discord setup panel."""

from __future__ import annotations

import functools

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
from daimon.core.github_connect_cards import (
    ADD_REPOS_LABEL,
    CONNECT_GITHUB_EMOJI,
    ask_manager_line,
    repo_count,
)
from daimon.core.github_panel import (
    GrantsPanel,
)
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.roster import RosterAgent
from daimon.core.stores.github_access import list_agent_repos
from daimon.core.stores.github_access_requests import list_asker_requests
from daimon.core.stores.github_links import account_link_status
from daimon.core.stores.github_personal_links import mint_link

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


HOME_AGENT_LIMIT = 10


def agent_count_line(agent_name: str, count: int) -> str:
    return f"{agent_name}: {repo_count(count)}" if count else f"{agent_name}: no repos yet"


class GitHubHomeView(PanelViewBase):
    """Every agent with how many repos it has, each with its own [Add repos]."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        agent_counts: tuple[tuple[RosterAgent, int], ...] = (),
        linked_login: str | None = None,
        own_waiting_count: int = 0,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        self.linked_login = linked_login
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        can_add = state.is_admin or state.can_manage_github_agents
        shown = agent_counts[:HOME_AGENT_LIMIT]
        lines = [agent_count_line(agent.name, count) for agent, count in shown]
        if len(agent_counts) > HOME_AGENT_LIMIT:
            lines.append(f"and {len(agent_counts) - HOME_AGENT_LIMIT} more")
        container.add_item(
            discord.ui.TextDisplay(
                "\n".join(["## GitHub", *lines] if lines else ["## GitHub", "No agents here yet."])
            )
        )
        # A classic embed can't put a button beside each line, so each [Add repos]
        # names its agent; five to a row.
        for start in range(0, len(shown) if can_add else 0, 5):
            row: EmbedActionRow = EmbedActionRow()
            for agent, _count in shown[start : start + 5]:
                add: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
                    label=f"{ADD_REPOS_LABEL} to {agent.name}"[:80],
                    style=discord.ButtonStyle.secondary,
                )
                add.callback = functools.partial(self._on_add_repos, agent=agent)  # type: ignore[method-assign]
                row.add_item(add)
            container.add_item(row)
        status = f"Linked as @{linked_login}" if linked_login else "GitHub isn't linked."
        container.add_item(discord.ui.TextDisplay(f"Personal link\n{status}"))
        actions: EmbedActionRow = EmbedActionRow()
        personal: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
            label="Use another account" if linked_login else "Link GitHub",
            style=discord.ButtonStyle.secondary,
        )
        personal.callback = self._on_personal_link  # type: ignore[method-assign]
        actions.add_item(personal)
        if linked_login:
            unlink: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
                label="Unlink", style=discord.ButtonStyle.secondary
            )
            unlink.callback = self._on_unlink_prompt  # type: ignore[method-assign]
            actions.add_item(unlink)
        if len(agent_counts) > HOME_AGENT_LIMIT and can_add:
            choose: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
                label="Choose agent", style=discord.ButtonStyle.secondary
            )
            choose.callback = self._on_choose  # type: ignore[method-assign]
            actions.add_item(choose)
        back: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
            label="◀ Back", style=discord.ButtonStyle.secondary
        )
        back.callback = self._on_back  # type: ignore[method-assign]
        actions.add_item(back)
        container.add_item(actions)
        if state.can_manage_github_agents or own_waiting_count:
            waiting_row: EmbedActionRow = EmbedActionRow()
            waiting: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
                label="Requests waiting", style=discord.ButtonStyle.secondary
            )
            waiting.callback = self._on_waiting  # type: ignore[method-assign]
            waiting_row.add_item(waiting)
            container.add_item(waiting_row)
        self.add_item(container)

    async def _on_add_repos(self, interaction: discord.Interaction, *, agent: RosterAgent) -> None:
        from daimon.adapters.discord.agent_setup.github_repos import (
            GitHubReposView,
            send_agent_connect_link,
        )

        gate = GitHubReposView(
            self.state,
            runtime=self.runtime,
            allowed_user_id=self.allowed_user_id,
            agent=agent,
            panel=GrantsPanel(mode="legacy", repos=(), working_repo=None, has_pat=False),
        )
        if not await gate.allowed(interaction):
            await interaction.followup.send(ask_manager_line(agent.name), ephemeral=True)
            return
        await send_agent_connect_link(
            interaction, runtime=self.runtime, state=self.state, agent=agent
        )

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
    counts: list[tuple[RosterAgent, int]] = []
    async with runtime.sessionmaker() as session:
        for agent in state.roster_agents:
            repos = await list_agent_repos(
                session,
                tenant_id=tenant_id,
                agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.ma_agent_id),
            )
            counts.append(
                (agent, sum(1 for repo in repos if not repo.staged and repo.status == "active"))
            )
        linked_login = await account_link_status(session, account_id=state.account_id)
        own_waiting = await list_asker_requests(
            session, tenant_id=tenant_id, account_id=state.account_id
        )
    return GitHubHomeView(
        state,
        runtime=runtime,
        allowed_user_id=user_id,
        agent_counts=tuple(counts),
        linked_login=linked_login,
        own_waiting_count=len(own_waiting),
    )
