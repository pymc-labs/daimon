"""Private GitHub entry screen in the Discord setup panel."""

from __future__ import annotations

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
    CONNECT_GITHUB_EMOJI,
    repo_count,
)
from daimon.core.github_panel import connect_link, sync_connect_admin
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.roster import RosterAgent
from daimon.core.stores.github_access import list_agent_repos
from daimon.core.stores.github_access_requests import list_asker_requests
from daimon.core.stores.github_links import account_link_status

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
        shown = agent_counts[:HOME_AGENT_LIMIT]
        lines = [agent_count_line(agent.name, count) for agent, count in shown]
        if len(agent_counts) > HOME_AGENT_LIMIT:
            lines.append(f"and {len(agent_counts) - HOME_AGENT_LIMIT} more")
        container.add_item(
            discord.ui.TextDisplay(
                "\n".join(["## GitHub", *lines] if lines else ["## GitHub", "No agents here yet."])
            )
        )
        status = f"Linked as @{linked_login}" if linked_login else "GitHub isn't linked."
        container.add_item(discord.ui.TextDisplay(f"Personal link\n{status}"))
        actions: EmbedActionRow = EmbedActionRow()
        personal: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
            label="Connect GitHub",
            style=discord.ButtonStyle.secondary,
        )
        personal.callback = self._on_personal_link  # type: ignore[method-assign]
        actions.add_item(personal)
        back: discord.ui.Button[GitHubHomeView] = discord.ui.Button(
            label="◀ Back", style=discord.ButtonStyle.secondary
        )
        back.callback = self._on_back  # type: ignore[method-assign]
        actions.add_item(back)
        container.add_item(actions)
        self.add_item(container)

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
        if not is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]
            await interaction.response.send_message(
                "Ask a server admin to connect GitHub.", ephemeral=True
            )
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        try:
            async with self.runtime.sessionmaker.begin() as session:
                await sync_connect_admin(
                    session,
                    tenant_id=tenant_id,
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                    verified_tenant_admin=True,
                )
                url = await connect_link(
                    session,
                    settings=self.runtime.settings,
                    tenant_id=tenant_id,
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                    verified_tenant_admin=True,
                    workspace_label=interaction.guild.name if interaction.guild else None,
                    requester_label=interaction.user.display_name,
                )
        except ValueError:
            await interaction.response.send_message(
                "GitHub didn't answer. Try again in a minute.", ephemeral=True
            )
            return
        view = connect_button_view(url, timeout=600)
        await interaction.response.send_message(
            embed=github_embed("Connect GitHub."),
            view=view,
            ephemeral=True,
        )


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
