"""GitHub repository grants inside the Discord agent setup panel."""

from __future__ import annotations

import uuid
from typing import Literal

from daimon.adapters.discord.agent_setup.github_embed_panel import (
    EmbedActionRow,
)
from daimon.adapters.discord.agent_setup.github_embed_panel import (
    GitHubEmbedPanel as PanelViewBase,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import channel_admin_caller, is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_reach import load_target_facts
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.github_panel import (
    GrantsPanel,
    activate_grants,
    load_grants_panel,
    remove_panel_grant,
    safe_github_error,
    stage_panel_grant,
)
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.operation_policy import TargetFacts, decide_operation
from daimon.core.roster import RosterAgent
from daimon.core.stores.accounts import get_account
from daimon.core.stores.github_access import deactivate_agent
from daimon.core.stores.github_connect import CLIENT_AGENT_MESSAGE

import discord


class GitHubReposView(PanelViewBase):
    """One agent's connected repos, with each write checked against live policy."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        agent: RosterAgent,
        panel: GrantsPanel,
        page: int = 0,
        settings: bool = False,
        settings_choice: Literal["ability", "remove"] | None = None,
        confirmation: Literal["remove", "turn_off"] | None = None,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        self.agent = agent
        self.panel = panel
        self.page = page
        self.settings = settings
        self.settings_choice: Literal["ability", "remove"] | None = settings_choice
        self.confirmation = confirmation
        start = (page // 20) * 20
        self.repo_id = panel.repos[page].repo_id if page < len(panel.repos) else None
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        if panel.saved_state:
            container.add_item(discord.ui.TextDisplay(CLIENT_AGENT_MESSAGE))
            back: discord.ui.Button[GitHubReposView] = discord.ui.Button(
                label="◀ Back", style=discord.ButtonStyle.secondary
            )
            back.callback = self._on_back  # type: ignore[method-assign]
            container.add_item(EmbedActionRow(back))
            self.add_item(container)
            return
        if confirmation is not None:
            if confirmation == "turn_off":
                message = f"Turn off GitHub for {agent.name}? It loses access to all its repos."
                label = "Turn off GitHub"
            else:
                repo_name = panel.repos[page].full_name if page < len(panel.repos) else "this repo"
                message = (
                    f"Remove {repo_name} from {agent.name}? It stops using it in new requests. "
                    "Chats that already used it keep what was said."
                )
                label = "Remove repo"
            container.add_item(discord.ui.TextDisplay(message))
            confirmation_actions: EmbedActionRow = EmbedActionRow()
            confirm: discord.ui.Button[GitHubReposView] = discord.ui.Button(
                label=label, style=discord.ButtonStyle.danger
            )
            confirm.callback = self._on_confirm  # type: ignore[method-assign]
            cancel: discord.ui.Button[GitHubReposView] = discord.ui.Button(
                label="◀ Back", style=discord.ButtonStyle.secondary
            )
            cancel.callback = self._on_cancel  # type: ignore[method-assign]
            confirmation_actions.add_item(confirm)
            confirmation_actions.add_item(cancel)
            container.add_item(confirmation_actions)
            self.add_item(container)
            return
        container.add_item(discord.ui.TextDisplay(panel.text(agent.name, page=start // 20)))
        if settings and settings_choice is None:
            menu: EmbedActionRow = EmbedActionRow()
            for choice, label in (
                ("ability", "Change what it can do"),
                ("remove", "Remove repos"),
            ):
                button: discord.ui.Button[GitHubReposView] = discord.ui.Button(
                    label=label, style=discord.ButtonStyle.secondary
                )
                button.callback = self._choose_settings(choice)  # type: ignore[method-assign]
                menu.add_item(button)
            container.add_item(menu)
        if settings and settings_choice is not None and panel.repos:
            if settings_choice == "ability":
                container.add_item(
                    discord.ui.TextDisplay(
                        "Read and write\nPush branches, open issues and pull requests.\n\n"
                        "Read only\nRead code, issues and pull requests."
                    )
                )
            options = [
                discord.SelectOption(
                    label=repo.full_name[:100],
                    value=str(repo.repo_id),
                    default=repo.repo_id == self.repo_id,
                )
                for repo in panel.repos[start : start + 20]
            ]
            select: discord.ui.Select[GitHubReposView] = discord.ui.Select(
                placeholder="Choose a repo", options=options
            )
            select.callback = self._on_select  # type: ignore[method-assign]
            container.add_item(EmbedActionRow(select))
            ceiling: EmbedActionRow = EmbedActionRow()
            for level in ("write", "read") if settings_choice == "ability" else ():
                button = discord.ui.Button(
                    label="Read only" if level == "read" else "Read and write",
                    style=discord.ButtonStyle.secondary,
                )
                button.callback = self._setter(level)  # type: ignore[method-assign]
                ceiling.add_item(button)
            if settings_choice == "remove":
                remove: discord.ui.Button[GitHubReposView] = discord.ui.Button(
                    label="Remove repos", style=discord.ButtonStyle.danger
                )
                remove.callback = self._on_remove  # type: ignore[method-assign]
                ceiling.add_item(remove)
            if ceiling.children:
                container.add_item(ceiling)
            if len(panel.repos) > 20:
                paging: EmbedActionRow = EmbedActionRow()
                previous: discord.ui.Button[GitHubReposView] = discord.ui.Button(
                    label="Previous repos",
                    style=discord.ButtonStyle.secondary,
                    disabled=start == 0,
                )
                previous.callback = self._on_previous  # type: ignore[method-assign]
                following: discord.ui.Button[GitHubReposView] = discord.ui.Button(
                    label="Next repos",
                    style=discord.ButtonStyle.secondary,
                    disabled=start + 20 >= len(panel.repos),
                )
                following.callback = self._on_next  # type: ignore[method-assign]
                paging.add_item(previous)
                paging.add_item(following)
                container.add_item(paging)
        actions: EmbedActionRow = EmbedActionRow()
        add_repos: discord.ui.Button[GitHubReposView] = discord.ui.Button(
            label="Add repos", style=discord.ButtonStyle.primary
        )
        add_repos.callback = self._on_add_repos  # type: ignore[method-assign]
        actions.add_item(add_repos)
        if panel.mode == "legacy":
            if panel.has_pending and settings:
                activate: discord.ui.Button[GitHubReposView] = discord.ui.Button(
                    label="Save changes", style=discord.ButtonStyle.primary
                )
                activate.callback = self._on_activate  # type: ignore[method-assign]
                actions.add_item(activate)
        else:
            if panel.has_pending and settings:
                activate: discord.ui.Button[GitHubReposView] = discord.ui.Button(
                    label="Save changes", style=discord.ButtonStyle.primary
                )
                activate.callback = self._on_activate  # type: ignore[method-assign]
                actions.add_item(activate)
            if settings and settings_choice is None:
                deactivate: discord.ui.Button[GitHubReposView] = discord.ui.Button(
                    label=f"Turn off GitHub for {agent.name}"[:80], style=discord.ButtonStyle.danger
                )
                deactivate.callback = self._on_deactivate  # type: ignore[method-assign]
                actions.add_item(deactivate)
        if not settings and any(repo.live_ceiling is not None for repo in panel.repos):
            settings_button: discord.ui.Button[GitHubReposView] = discord.ui.Button(
                label="Settings", style=discord.ButtonStyle.secondary
            )
            settings_button.callback = self._on_settings  # type: ignore[method-assign]
            actions.add_item(settings_button)
        back: discord.ui.Button[GitHubReposView] = discord.ui.Button(
            label="◀ Back", style=discord.ButtonStyle.secondary
        )
        back.callback = self._on_back  # type: ignore[method-assign]
        actions.add_item(back)
        container.add_item(actions)
        self.add_item(container)

    def _ids(self) -> tuple[uuid.UUID, uuid.UUID]:
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        return tenant_id, derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=self.agent.ma_agent_id)

    async def allowed(self, interaction: discord.Interaction) -> bool:
        await interaction.response.defer()
        if interaction.guild_id != self.state.guild_id:
            return False
        tenant_id, agent_id = self._ids()
        live_agent = await find_agent_by_derived_uuid(
            self.runtime.anthropic, tenant_id=tenant_id, agent_id=agent_id
        )
        if live_agent is None:
            return False
        managed = live_agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
        admin = is_guild_admin(interaction)  # pyright: ignore[reportArgumentType]
        async with self.runtime.sessionmaker() as session:
            account = await get_account(session, self.state.account_id)
            if account is None or account.is_external or account.tenant_id != tenant_id:
                return False
            caller = channel_admin_caller(interaction.user).model_copy(
                update={"is_server_admin": admin}
            )
            facts = (
                await load_target_facts(
                    session,
                    "github_grant",
                    tenant_id=tenant_id,
                    platform="discord",
                    agent_names=(self.agent.name, live_agent.name),
                    ma_agent_id=str(live_agent.id),
                    default=self.runtime.deployment_default,
                    caller=caller,
                    is_daimon_managed=managed,
                    caller_platform_user_id=str(interaction.user.id),
                )
                if not admin
                else TargetFacts(is_daimon_managed=managed, is_reachable_in_tenant=False)
            )
        return decide_operation("github_grant", is_admin=admin, target=facts) == "allow"

    async def _refresh_panel(self, interaction: discord.Interaction) -> None:
        tenant_id, agent_id = self._ids()
        async with self.runtime.sessionmaker() as session:
            panel = await load_grants_panel(
                session, tenant_id=tenant_id, agent_id=agent_id, agent_name=self.agent.name
            )
        await self.swap_to(
            interaction,
            GitHubReposView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                agent=self.agent,
                panel=panel,
                page=self.page,
                settings=self.settings,
                settings_choice=self.settings_choice,
            ),
        )

    async def _on_settings(self, interaction: discord.Interaction) -> None:
        if not await self.allowed(interaction):
            await interaction.followup.send(
                "You cannot change this agent's GitHub repos.", ephemeral=True
            )
            return
        await self.swap_to(
            interaction,
            GitHubReposView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                agent=self.agent,
                panel=self.panel,
                page=self.page,
                settings=True,
            ),
        )

    def _choose_settings(self, choice: Literal["ability", "remove"]):
        async def callback(interaction: discord.Interaction) -> None:
            if not await self.allowed(interaction):
                return
            await self.swap_to(
                interaction,
                GitHubReposView(
                    self.state,
                    runtime=self.runtime,
                    allowed_user_id=self.allowed_user_id,
                    agent=self.agent,
                    panel=self.panel,
                    page=self.page,
                    settings=True,
                    settings_choice=choice,
                ),
            )

        return callback

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if not await self.allowed(interaction):
            await interaction.followup.send(
                "You cannot view this agent's GitHub repos.", ephemeral=True
            )
            return
        selected = next(
            (item for item in self.walk_children() if isinstance(item, discord.ui.Select)), None
        )
        if selected is None or not selected.values:
            return
        repo_id = int(selected.values[0])
        self.page = next(
            (i for i, repo in enumerate(self.panel.repos) if repo.repo_id == repo_id), 0
        )
        await self._refresh_panel(interaction)

    async def _on_previous(self, interaction: discord.Interaction) -> None:
        if not await self.allowed(interaction):
            await interaction.followup.send(
                "You cannot view this agent's GitHub repos.", ephemeral=True
            )
            return
        self.page = max(0, (self.page // 20 - 1) * 20)
        await self._refresh_panel(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if not await self.allowed(interaction):
            await interaction.followup.send(
                "You cannot view this agent's GitHub repos.", ephemeral=True
            )
            return
        self.page = min(len(self.panel.repos) - 1, (self.page // 20 + 1) * 20)
        await self._refresh_panel(interaction)

    def _setter(self, level: Literal["read", "write"]):
        async def callback(interaction: discord.Interaction) -> None:
            if not await self.allowed(interaction):
                await interaction.followup.send(
                    "You cannot change this agent's GitHub repos.", ephemeral=True
                )
                return
            if self.repo_id is None:
                return
            repo = self.panel.repos[self.page]
            tenant_id, agent_id = self._ids()
            try:
                async with self.runtime.sessionmaker.begin() as session:
                    await stage_panel_grant(
                        session,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        repo_id=repo.repo_id,
                        baseline_access=level,
                        ceiling_access=level,
                        account_id=self.state.account_id,
                        is_working_repo=repo.working
                        or (
                            self.panel.working_repo is not None
                            and repo.full_name.casefold() == self.panel.working_repo.casefold()
                        ),
                    )
                    if self.panel.mode == "app":
                        await activate_grants(
                            session,
                            tenant_id=tenant_id,
                            agent_id=agent_id,
                            account_id=self.state.account_id,
                            agent_name=self.agent.name,
                        )
            except ValueError as error:
                await interaction.followup.send(safe_github_error(error), ephemeral=True)
                return
            await self._refresh_panel(interaction)

        return callback

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        await self._confirm(interaction, "remove")

    async def _confirm(
        self,
        interaction: discord.Interaction,
        choice: Literal["remove", "turn_off"],
    ) -> None:
        await self.swap_to(
            interaction,
            GitHubReposView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                agent=self.agent,
                panel=self.panel,
                page=self.page,
                confirmation=choice,
            ),
        )

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await self._refresh_panel(interaction)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        if not await self.allowed(interaction):
            await interaction.followup.send(
                "You cannot change this agent's GitHub repos.", ephemeral=True
            )
            return
        tenant_id, agent_id = self._ids()
        try:
            async with self.runtime.sessionmaker.begin() as session:
                if self.confirmation == "remove":
                    if self.repo_id is None:
                        return
                    await remove_panel_grant(
                        session,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        repo_id=self.repo_id,
                        account_id=self.state.account_id,
                    )
                    if self.panel.mode == "app":
                        await activate_grants(
                            session,
                            tenant_id=tenant_id,
                            agent_id=agent_id,
                            account_id=self.state.account_id,
                            agent_name=self.agent.name,
                        )
                elif self.confirmation == "turn_off":
                    await deactivate_agent(
                        session,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        changed_by_account_id=self.state.account_id,
                    )
        except ValueError as error:
            await interaction.followup.send(safe_github_error(error), ephemeral=True)
            return
        await self._refresh_panel(interaction)

    async def _on_add_repos(self, interaction: discord.Interaction) -> None:
        if not await self.allowed(interaction):
            await interaction.followup.send(
                "You cannot change this agent's GitHub repos.", ephemeral=True
            )
            return
        from daimon.adapters.discord.agent_setup.github_add_repos import GitHubAddReposView

        await self.swap_to(
            interaction,
            GitHubAddReposView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                agent=self.agent,
                panel=self.panel,
            ),
        )

    async def _on_activate(self, interaction: discord.Interaction) -> None:
        if not await self.allowed(interaction):
            await interaction.followup.send(
                "You cannot change this agent's GitHub repos.", ephemeral=True
            )
            return
        tenant_id, agent_id = self._ids()
        try:
            async with self.runtime.sessionmaker.begin() as session:
                removed_pat = await activate_grants(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    account_id=self.state.account_id,
                    agent_name=self.agent.name,
                )
        except ValueError as error:
            await interaction.followup.send(safe_github_error(error), ephemeral=True)
            return
        await self._refresh_panel(interaction)
        if removed_pat:
            await interaction.followup.send(
                f"Updated {self.agent.name}. Open chats restart on the next turn.",
                ephemeral=True,
            )

    async def _on_deactivate(self, interaction: discord.Interaction) -> None:
        await self._confirm(interaction, "turn_off")

    async def _on_back(self, interaction: discord.Interaction) -> None:
        if self.settings:
            if not await self.allowed(interaction):
                return
            await self.swap_to(
                interaction,
                GitHubReposView(
                    self.state,
                    runtime=self.runtime,
                    allowed_user_id=self.allowed_user_id,
                    agent=self.agent,
                    panel=self.panel,
                    page=self.page,
                    settings=self.settings_choice is not None,
                ),
            )
            return
        from daimon.adapters.discord.agent_setup.details_view import DetailsView

        await self.swap_to(
            interaction,
            DetailsView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                agent=self.agent,
            ),
        )
