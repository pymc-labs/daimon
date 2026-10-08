"""Server-wide connected GitHub repos in Discord."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from daimon.adapters.discord.agent_setup.github_embed_panel import (
    EmbedActionRow,
)
from daimon.adapters.discord.agent_setup.github_embed_panel import (
    GitHubEmbedPanel as PanelViewBase,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.github_panel import safe_github_error
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.accounts import get_account
from daimon.core.stores.github_access import AuthorizedRepo, list_authorized_repos
from daimon.core.stores.github_connected_repos import (
    disconnect_github,
    disconnect_repo,
    set_repo_ability,
)

import discord


class GitHubManageView(PanelViewBase):
    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        repos: tuple[AuthorizedRepo, ...],
        selected_id: int | None = None,
        step: Literal["list", "change", "disconnect", "disconnect_all"] = "list",
        page: int = 0,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        self.repos = repos
        self.selected_id = selected_id or (repos[0].repo_id if repos else None)
        self.step = step
        self.page = page
        selected = next((repo for repo in repos if repo.repo_id == self.selected_id), None)
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        if step == "disconnect_all":
            container.add_item(
                discord.ui.TextDisplay(
                    "Disconnect GitHub from this server? Agents lose access to these repos "
                    "right away. "
                    "Waiting requests are cancelled.\n"
                    "Chats, and changes already made on GitHub, stay. "
                    "To remove Daimon from GitHub too:"
                )
            )
            container.add_item(
                EmbedActionRow(
                    discord.ui.Button(
                        label="Open GitHub settings ↗",
                        url="https://github.com/settings/installations",
                    )
                )
            )
            self._button_row(
                container,
                (
                    ("Disconnect", self._on_disconnect_all, discord.ButtonStyle.danger),
                    ("◀ Back", self._on_back, discord.ButtonStyle.secondary),
                ),
            )
        elif step == "disconnect" and selected is not None:
            container.add_item(
                discord.ui.TextDisplay(
                    f"Disconnect {selected.repo_full_name}? Agents using it lose it."
                )
            )
            self._button_row(
                container,
                (
                    ("Disconnect", self._on_disconnect, discord.ButtonStyle.danger),
                    ("◀ Back", self._on_back, discord.ButtonStyle.secondary),
                ),
            )
        elif step == "change" and selected is not None:
            container.add_item(
                discord.ui.TextDisplay(
                    "Read and write\nPush branches, open issues and pull requests.\n\n"
                    "Read only\nRead code, issues and pull requests."
                )
            )
            container.add_item(
                discord.ui.TextDisplay(
                    f"{selected.repo_full_name}\nAgents can: "
                    + ("Read only" if selected.max_access == "read" else "Read and write")
                )
            )
            self._button_row(
                container,
                (
                    (
                        "Read and write",
                        self._set_write,
                        discord.ButtonStyle.secondary,
                    ),
                    ("Read only", self._set_read, discord.ButtonStyle.secondary),
                    ("◀ Back", self._on_back, discord.ButtonStyle.secondary),
                ),
            )
        else:
            page_repos = repos[page * 20 : (page + 1) * 20]
            lines = ["## Manage connected repos"]
            lines.extend(
                f"• {repo.repo_full_name} — "
                + ("Read only" if repo.max_access == "read" else "Read and write")
                for repo in page_repos
            )
            container.add_item(discord.ui.TextDisplay("\n".join(lines)))
            if page_repos:
                selector: discord.ui.Select[GitHubManageView] = discord.ui.Select(
                    placeholder="Select repo…",
                    options=[
                        discord.SelectOption(
                            label=repo.repo_full_name[:100],
                            value=str(repo.repo_id),
                            default=repo.repo_id == self.selected_id,
                        )
                        for repo in page_repos
                    ],
                )
                selector.callback = self._on_select  # type: ignore[method-assign]
                container.add_item(EmbedActionRow(selector))
                self._button_row(
                    container,
                    (
                        ("Change", self._on_change, discord.ButtonStyle.secondary),
                        ("Disconnect", self._on_confirm_disconnect, discord.ButtonStyle.danger),
                        ("◀ Back", self._on_back, discord.ButtonStyle.secondary),
                    ),
                )
            else:
                self._button_row(
                    container, (("◀ Back", self._on_back, discord.ButtonStyle.secondary),)
                )
            if len(repos) > 20:
                self._button_row(
                    container,
                    (
                        ("Previous repos", self._on_previous, discord.ButtonStyle.secondary),
                        ("Next repos", self._on_next, discord.ButtonStyle.secondary),
                    ),
                )
            if repos:
                self._button_row(
                    container,
                    (
                        (
                            "Disconnect GitHub",
                            self._on_confirm_disconnect_all,
                            discord.ButtonStyle.danger,
                        ),
                    ),
                )
        self.add_item(container)

    def _button_row(
        self,
        container: discord.ui.Container[discord.ui.LayoutView],
        items: tuple[
            tuple[str, Callable[[discord.Interaction], Awaitable[None]], discord.ButtonStyle],
            ...,
        ],
    ) -> None:
        row: EmbedActionRow = EmbedActionRow()
        for label, callback, style in items:
            button: discord.ui.Button[GitHubManageView] = discord.ui.Button(
                label=label, style=style
            )
            button.callback = callback  # type: ignore[method-assign]
            row.add_item(button)
        container.add_item(row)

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != self.state.guild_id or not is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]
            await interaction.response.send_message(
                "Only a server admin can manage connected repos.", ephemeral=True
            )
            return False
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        async with self.runtime.sessionmaker() as session:
            account = await get_account(session, self.state.account_id)
        if account is None or account.is_external or account.tenant_id != tenant_id:
            await interaction.response.send_message(
                "Only a server admin can manage connected repos.", ephemeral=True
            )
            return False
        return True

    async def _swap(
        self,
        interaction: discord.Interaction,
        *,
        step: Literal["list", "change", "disconnect", "disconnect_all"] = "list",
    ) -> None:
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        async with self.runtime.sessionmaker() as session:
            repos = tuple(
                r
                for r in await list_authorized_repos(session, tenant_id=tenant_id)
                if r.status == "active"
            )
        await self.swap_to(
            interaction,
            GitHubManageView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                repos=repos,
                selected_id=self.selected_id,
                step=step,
                page=self.page,
            ),
        )

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            return
        selector = next(
            (item for item in self.walk_children() if isinstance(item, discord.ui.Select)), None
        )
        if selector is None or not selector.values:
            return
        selected_id = int(selector.values[0])
        if selected_id not in {repo.repo_id for repo in self.repos}:
            return
        self.selected_id = selected_id
        await self._swap(interaction)

    async def _on_change(self, interaction: discord.Interaction) -> None:
        if await self._allowed(interaction):
            await self._swap(interaction, step="change")

    async def _on_confirm_disconnect(self, interaction: discord.Interaction) -> None:
        if await self._allowed(interaction):
            await self._swap(interaction, step="disconnect")

    async def _on_confirm_disconnect_all(self, interaction: discord.Interaction) -> None:
        if await self._allowed(interaction):
            await self._swap(interaction, step="disconnect_all")

    async def _on_disconnect_all(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction) or self.step != "disconnect_all":
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        try:
            async with self.runtime.sessionmaker.begin() as session:
                await disconnect_github(
                    session, tenant_id=tenant_id, account_id=self.state.account_id
                )
        except ValueError as error:
            await interaction.response.send_message(safe_github_error(error), ephemeral=True)
            return
        from daimon.adapters.discord.agent_setup.github_home import load_home

        home = await load_home(self.state, runtime=self.runtime, user_id=interaction.user.id)
        await self.swap_to(interaction, home)

    async def _set(
        self, interaction: discord.Interaction, ability: Literal["read", "write"]
    ) -> None:
        if not await self._allowed(interaction) or self.selected_id is None:
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        try:
            async with self.runtime.sessionmaker.begin() as session:
                await set_repo_ability(
                    session,
                    tenant_id=tenant_id,
                    repo_id=self.selected_id,
                    ability=ability,
                    account_id=self.state.account_id,
                )
        except ValueError as error:
            await interaction.response.send_message(safe_github_error(error), ephemeral=True)
            return
        await self._swap(interaction)

    async def _set_read(self, interaction: discord.Interaction) -> None:
        await self._set(interaction, "read")

    async def _set_write(self, interaction: discord.Interaction) -> None:
        await self._set(interaction, "write")

    async def _on_disconnect(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction) or self.selected_id is None:
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        try:
            async with self.runtime.sessionmaker.begin() as session:
                await disconnect_repo(
                    session,
                    tenant_id=tenant_id,
                    repo_id=self.selected_id,
                    account_id=self.state.account_id,
                )
        except ValueError as error:
            await interaction.response.send_message(safe_github_error(error), ephemeral=True)
            return
        self.selected_id = None
        await self._swap(interaction)

    async def _on_previous(self, interaction: discord.Interaction) -> None:
        if await self._allowed(interaction):
            self.page = max(0, self.page - 1)
            await self._swap(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if await self._allowed(interaction):
            self.page += 1
            await self._swap(interaction)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            return
        if self.step != "list":
            await self._swap(interaction)
            return
        from daimon.adapters.discord.agent_setup.github_home import load_home

        home = await load_home(self.state, runtime=self.runtime, user_id=interaction.user.id)
        await self.swap_to(interaction, home)
