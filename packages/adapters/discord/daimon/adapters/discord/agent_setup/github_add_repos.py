"""Pick, review, and add connected repos to one Discord agent."""

from __future__ import annotations

from typing import Literal, cast

from daimon.adapters.discord.agent_setup.github_embed_panel import (
    EmbedActionRow,
)
from daimon.adapters.discord.agent_setup.github_embed_panel import (
    GitHubEmbedPanel as PanelViewBase,
)
from daimon.adapters.discord.agent_setup.github_repos import GitHubReposView
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.github_panel import (
    GrantsPanel,
    RepoChoice,
    activate_grants,
    connect_link,
    load_grants_panel,
    safe_github_error,
    stage_panel_grant,
    suggested_repo,
    sync_connect_admin,
)
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.roster import RosterAgent
from daimon.core.stores.github_connect import CLIENT_AGENT_MESSAGE

import discord

Step = Literal["pick", "change"]


class GitHubAddReposView(PanelViewBase):
    """One private draft whose choices survive paging and review."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        agent: RosterAgent,
        panel: GrantsPanel,
        selected_ids: frozenset[int] | None = None,
        ability: Literal["read", "write"] = "write",
        page: int = 0,
        step: Step = "pick",
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        self.agent = agent
        self.panel = panel
        self.page = page
        self.step = step
        self.ability = ability
        if selected_ids is None:
            selected_ids = frozenset()
        self.selected_ids = selected_ids
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        if panel.saved_state:
            container.add_item(discord.ui.TextDisplay(CLIENT_AGENT_MESSAGE))
            actions: EmbedActionRow = EmbedActionRow()
            self._add_back(actions)
            container.add_item(actions)
            self.add_item(container)
            return
        if step == "pick":
            container.add_item(
                discord.ui.TextDisplay(f"## Repos {agent.name} uses\nCan: {self._ability_label()}")
            )
            page_repos = panel.repos[page * 20 : (page + 1) * 20]
            if page_repos:
                select: discord.ui.Select[GitHubAddReposView] = discord.ui.Select(
                    placeholder="Select repos…",
                    min_values=0,
                    max_values=len(page_repos),
                    options=[
                        discord.SelectOption(
                            label=repo.full_name[:100],
                            value=str(repo.repo_id),
                            default=repo.repo_id in selected_ids,
                        )
                        for repo in page_repos
                    ],
                )
                select.callback = self._on_select  # type: ignore[method-assign]
                container.add_item(EmbedActionRow(select))
            else:
                container.add_item(discord.ui.TextDisplay("No repos connected here."))
            suggestion = suggested_repo(panel.repos, state.channel_name)
            if suggestion is not None and suggestion.repo_id not in selected_ids:
                container.add_item(discord.ui.TextDisplay(f"Suggested: {suggestion.full_name}"))
                suggested: discord.ui.Button[GitHubAddReposView] = discord.ui.Button(
                    label="Add", style=discord.ButtonStyle.secondary
                )
                suggested.callback = self._add_suggestion(suggestion.repo_id)  # type: ignore[method-assign]
                container.add_item(EmbedActionRow(suggested))
            if not state.is_admin:
                container.add_item(
                    discord.ui.TextDisplay("Repo missing? Ask a server admin to connect it.")
                )
            actions: EmbedActionRow = EmbedActionRow()
            change: discord.ui.Button[GitHubAddReposView] = discord.ui.Button(
                label="Change", style=discord.ButtonStyle.secondary
            )
            change.callback = self._on_change  # type: ignore[method-assign]
            actions.add_item(change)
            if panel.repos:
                add: discord.ui.Button[GitHubAddReposView] = discord.ui.Button(
                    label="Add repos", style=discord.ButtonStyle.primary
                )
                add.callback = self._on_add  # type: ignore[method-assign]
                actions.add_item(add)
            if state.is_admin:
                connect: discord.ui.Button[GitHubAddReposView] = discord.ui.Button(
                    label="Connect more repos" if panel.repos else "Connect GitHub",
                    style=(
                        discord.ButtonStyle.secondary
                        if panel.repos
                        else discord.ButtonStyle.primary
                    ),
                )
                connect.callback = self._on_connect_more  # type: ignore[method-assign]
                actions.add_item(connect)
            self._add_back(actions)
            container.add_item(actions)
            if len(panel.repos) > 20:
                nav: EmbedActionRow = EmbedActionRow()
                previous: discord.ui.Button[GitHubAddReposView] = discord.ui.Button(
                    label="Previous repos",
                    disabled=page == 0,
                    style=discord.ButtonStyle.secondary,
                )
                previous.callback = self._on_previous  # type: ignore[method-assign]
                following: discord.ui.Button[GitHubAddReposView] = discord.ui.Button(
                    label="Next repos",
                    disabled=(page + 1) * 20 >= len(panel.repos),
                    style=discord.ButtonStyle.secondary,
                )
                following.callback = self._on_next  # type: ignore[method-assign]
                nav.add_item(previous)
                nav.add_item(following)
                container.add_item(nav)
        elif step == "change":
            container.add_item(discord.ui.TextDisplay("## Change what it can do"))
            container.add_item(
                discord.ui.TextDisplay(
                    "Read and write\nPush branches, open issues and pull requests.\n\n"
                    "Read only\nRead code, issues and pull requests."
                )
            )
            actions = EmbedActionRow()
            for label, ability_choice in (
                ("Read and write", "write"),
                ("Read only", "read"),
            ):
                button: discord.ui.Button[GitHubAddReposView] = discord.ui.Button(
                    label=label, style=discord.ButtonStyle.secondary
                )
                button.callback = self._choose_ability(ability_choice)  # type: ignore[method-assign]
                actions.add_item(button)
            container.add_item(actions)
            back_actions: EmbedActionRow = EmbedActionRow()
            self._add_back(back_actions, target="pick")
            container.add_item(back_actions)
        self.add_item(container)

    def _ability_label(self) -> str:
        return "Read only" if self.ability == "read" else "Read and write"

    def _effective_ability(self, repo: RepoChoice) -> str:
        return (
            "Read only"
            if self.ability == "read" or repo.max_access == "read"
            else self._ability_label()
        )

    def _add_back(self, row: EmbedActionRow, *, target: Step | None = None) -> None:
        button: discord.ui.Button[GitHubAddReposView] = discord.ui.Button(
            label="◀ Back", style=discord.ButtonStyle.secondary
        )
        button.callback = self._back_to(target)  # type: ignore[method-assign]
        row.add_item(button)

    async def _authorized(self, interaction: discord.Interaction) -> bool:
        gate = GitHubReposView(
            self.state,
            runtime=self.runtime,
            allowed_user_id=self.allowed_user_id,
            agent=self.agent,
            panel=self.panel,
        )
        if await gate.allowed(interaction):
            return True
        await interaction.followup.send(
            "You cannot change this agent's GitHub repos.", ephemeral=True
        )
        return False

    async def _swap(self, interaction: discord.Interaction, **changes: object) -> None:
        args: dict[str, object] = {
            "selected_ids": self.selected_ids,
            "ability": self.ability,
            "page": self.page,
            "step": self.step,
        }
        args.update(changes)
        view = GitHubAddReposView(
            self.state,
            runtime=self.runtime,
            allowed_user_id=self.allowed_user_id,
            agent=self.agent,
            panel=self.panel,
            selected_ids=cast(frozenset[int], args["selected_ids"]),
            ability=cast(Literal["read", "write"], args["ability"]),
            page=cast(int, args["page"]),
            step=cast(Step, args["step"]),
        )
        await self.swap_to(interaction, view)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction):
            return
        selected = next(
            (item for item in self.walk_children() if isinstance(item, discord.ui.Select)), None
        )
        if selected is None:
            return
        visible = {repo.repo_id for repo in self.panel.repos[self.page * 20 : (self.page + 1) * 20]}
        ids = (self.selected_ids - visible) | {int(value) for value in selected.values}
        await self._swap(interaction, selected_ids=frozenset(ids))

    async def _on_previous(self, interaction: discord.Interaction) -> None:
        if await self._authorized(interaction):
            await self._swap(interaction, page=max(0, self.page - 1))

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if await self._authorized(interaction):
            await self._swap(interaction, page=self.page + 1)

    def _add_suggestion(self, repo_id: int):
        async def callback(interaction: discord.Interaction) -> None:
            if await self._authorized(interaction):
                await self._swap(interaction, selected_ids=frozenset({*self.selected_ids, repo_id}))

        return callback

    async def _on_change(self, interaction: discord.Interaction) -> None:
        if await self._authorized(interaction):
            await self._swap(interaction, step="change")

    async def _on_connect_more(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.state.guild_id or not is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]
            await interaction.response.send_message(
                "Only a server admin can connect repos.", ephemeral=True
            )
            return
        from daimon.adapters.discord.agent_setup.github_card_ui import github_embed
        from daimon.adapters.discord.agent_setup.github_home import GitHubLinkView

        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
        agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=self.agent.ma_agent_id)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with self.runtime.sessionmaker.begin() as session:
                await sync_connect_admin(
                    session,
                    tenant_id=tenant_id,
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                    verified_tenant_admin=is_guild_admin(interaction),  # pyright: ignore[reportArgumentType]
                )
                url = await connect_link(
                    session,
                    settings=self.runtime.settings,
                    tenant_id=tenant_id,
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                    verified_tenant_admin=is_guild_admin(interaction),  # pyright: ignore[reportArgumentType]
                    requester_label=interaction.user.display_name,
                    agent_id=agent_id,
                    agent_name=self.agent.name,
                )
        except ValueError as error:
            await interaction.followup.send(safe_github_error(error), ephemeral=True)
            return
        await interaction.followup.send(
            embed=github_embed(
                f"Connect GitHub for {self.agent.name}.",
                state="waiting",
            ),
            view=GitHubLinkView(url, user_id=interaction.user.id),
            ephemeral=True,
        )

    def _choose_ability(self, choice: str):
        async def callback(interaction: discord.Interaction) -> None:
            if await self._authorized(interaction):
                await self._swap(interaction, ability=choice, step="pick")

        return callback

    def _back_to(self, target: Step | None):
        async def callback(interaction: discord.Interaction) -> None:
            if not await self._authorized(interaction):
                return
            if target is not None:
                await self._swap(interaction, step=target)
            else:
                await self.swap_to(
                    interaction,
                    GitHubReposView(
                        self.state,
                        runtime=self.runtime,
                        allowed_user_id=self.allowed_user_id,
                        agent=self.agent,
                        panel=self.panel,
                    ),
                )

        return callback

    async def _on_add(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction):
            return
        if not self.selected_ids:
            await interaction.followup.send("Select at least one repo.", ephemeral=True)
            return
        if self.panel.saved_state:
            await interaction.followup.send(CLIENT_AGENT_MESSAGE, ephemeral=True)
            return
        await self._commit(interaction)

    async def _commit(self, interaction: discord.Interaction) -> None:
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=self.agent.ma_agent_id)
        try:
            async with self.runtime.sessionmaker.begin() as session:
                current = await load_grants_panel(
                    session, tenant_id=tenant_id, agent_id=agent_id, agent_name=self.agent.name
                )
                chosen = [repo for repo in current.repos if repo.repo_id in self.selected_ids]
                if len(chosen) != len(self.selected_ids):
                    raise ValueError("A repo is no longer connected here. Review your choices.")
                for repo in chosen:
                    ability: Literal["read", "write"] = (
                        "read" if self.ability == "read" or repo.max_access == "read" else "write"
                    )
                    await stage_panel_grant(
                        session,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        repo_id=repo.repo_id,
                        baseline_access=ability,
                        ceiling_access=ability,
                        account_id=self.state.account_id,
                        is_working_repo=repo.working
                        or (
                            current.working_repo is not None
                            and repo.full_name.casefold() == current.working_repo.casefold()
                        ),
                    )
                removed_key = await activate_grants(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    account_id=self.state.account_id,
                    agent_name=self.agent.name,
                )
                current = await load_grants_panel(
                    session, tenant_id=tenant_id, agent_id=agent_id, agent_name=self.agent.name
                )
        except ValueError as error:
            await interaction.followup.send(safe_github_error(error), ephemeral=True)
            return
        await self.swap_to(
            interaction,
            GitHubReposView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                agent=self.agent,
                panel=current,
            ),
        )
        if removed_key:
            await interaction.followup.send(
                f"Updated {self.agent.name}. Open chats restart on the next turn.",
                ephemeral=True,
            )
