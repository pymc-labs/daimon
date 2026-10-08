"""Requests waiting under the private Discord GitHub screen."""

from __future__ import annotations

import uuid

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
from daimon.core.github_panel import connect_link, safe_github_error
from daimon.core.github_request_cards import admin_card
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.operation_policy import TargetFacts, decide_operation
from daimon.core.stores.github_access import AuthorizedRepo, list_authorized_repos
from daimon.core.stores.github_access_requests import (
    AccessRequest,
    cancel_request,
    dismiss_delivery,
    list_asker_requests,
    list_waiting,
    set_status,
)
from daimon.core.stores.github_links import account_link_status
from daimon.core.stores.github_personal_links import mint_link
from daimon.core.stores.github_request_actions import (
    approve_connected_request,
    approve_connection_request,
)

import discord


async def _may_manage_request(
    runtime: DiscordRuntime,
    *,
    row: AccessRequest,
    member: discord.Member,
    is_admin: bool,
) -> bool:
    live_agent = await find_agent_by_derived_uuid(
        runtime.anthropic, tenant_id=row.tenant_id, agent_id=row.agent_id
    )
    if live_agent is None or live_agent.id != row.ma_agent_id:
        return False
    managed = live_agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
    async with runtime.sessionmaker() as session:
        facts = (
            TargetFacts(is_daimon_managed=managed, is_reachable_in_tenant=False)
            if is_admin
            else await load_target_facts(
                session,
                "github_grant",
                tenant_id=row.tenant_id,
                platform="discord",
                agent_names=(row.agent_name, live_agent.name),
                ma_agent_id=str(live_agent.id),
                default=runtime.deployment_default,
                caller=channel_admin_caller(member),
                is_daimon_managed=managed,
                caller_platform_user_id=str(member.id),
            )
        )
    return decide_operation("github_grant", is_admin=is_admin, target=facts) == "allow"


class GitHubWaitingView(PanelViewBase):
    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        requests: tuple[AccessRequest, ...],
        own_requests: tuple[AccessRequest, ...],
        repos: tuple[AuthorizedRepo, ...],
        selected_id: uuid.UUID | None = None,
        page: int = 0,
        requester_labels: dict[str, str] | None = None,
        thread_labels: dict[str, str] | None = None,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        self.requests = requests
        self.own_requests = own_requests
        self.selected_id = selected_id
        self.page = min(max(page, 0), max(0, (len(requests) - 1) // 20))
        self._repos = repos
        self.requester_labels = requester_labels or {}
        self.thread_labels = thread_labels or {}
        selected = next((row for row in requests if row.id == selected_id), None)
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        if selected is None:
            lines = ["## Requests waiting"]
            if not requests:
                lines.append("Nothing waiting.")
            page_requests = requests[self.page * 20 : (self.page + 1) * 20]
            for row in page_requests:
                where = (
                    "in a direct message"
                    if row.parent_channel_id == row.thread_id
                    else f"in <#{row.parent_channel_id}>"
                )
                lines.append(
                    f"• <@{row.requester_platform_user_id}>\n{row.agent_name}\n{where}\n"
                    f"<t:{int(row.created_at.timestamp())}:R>"
                )
            container.add_item(discord.ui.TextDisplay("\n".join(lines)))
            if requests:
                select: discord.ui.Select[GitHubWaitingView] = discord.ui.Select(
                    placeholder="Review a request",
                    options=[
                        discord.SelectOption(
                            label=row.agent_name[:100],
                            description=(
                                "Requested by "
                                + self.requester_labels.get(
                                    row.requester_platform_user_id, "a member"
                                )
                            )[:100],
                            value=str(row.id),
                        )
                        for row in page_requests
                    ],
                )
                select.callback = self._on_select  # type: ignore[method-assign]
                container.add_item(EmbedActionRow(select))
                if len(requests) > 20:
                    paging: EmbedActionRow = EmbedActionRow()
                    previous: discord.ui.Button[GitHubWaitingView] = discord.ui.Button(
                        label="Previous requests", disabled=self.page == 0
                    )
                    previous.callback = self._on_previous  # type: ignore[method-assign]
                    following: discord.ui.Button[GitHubWaitingView] = discord.ui.Button(
                        label="Next requests", disabled=(self.page + 1) * 20 >= len(requests)
                    )
                    following.callback = self._on_next  # type: ignore[method-assign]
                    paging.add_item(previous)
                    paging.add_item(following)
                    container.add_item(paging)
            if own_requests:
                container.add_item(discord.ui.TextDisplay("Your requests waiting"))
                select_own: discord.ui.Select[GitHubWaitingView] = discord.ui.Select(
                    placeholder="Cancel request",
                    options=[
                        discord.SelectOption(
                            label=row.agent_name[:100],
                            description=self.thread_labels.get(row.thread_id, "This thread")[:100],
                            value=str(row.id),
                        )
                        for row in own_requests[:20]
                    ],
                )
                select_own.callback = self._on_cancel_select  # type: ignore[method-assign]
                container.add_item(EmbedActionRow(select_own))
        else:
            connected = {
                repo.repo_full_name.casefold(): repo
                for repo in self._repos
                if repo.status == "active"
            }
            all_connected = all(name.casefold() in connected for name in selected.repo_names)
            card = admin_card(
                selected,
                connected_names=tuple(selected.repo_names) if all_connected else (),
                channel_label=(
                    ""
                    if selected.parent_channel_id == selected.thread_id
                    else f"<#{selected.parent_channel_id}>"
                ),
                requester_label=f"<@{selected.requester_platform_user_id}>",
                ability=selected.required_ability,
            )
            container.add_item(discord.ui.TextDisplay(card.text))
            actions_row: EmbedActionRow = EmbedActionRow()
            choices = [
                (
                    label,
                    self._on_decline if label == "Decline" else self._on_hide,
                    discord.ButtonStyle.secondary,
                )
                for label in card.secondary
            ]
            if card.primary:
                choices.insert(0, (card.primary, self._on_approve, discord.ButtonStyle.primary))
            for label, callback, style in choices:
                button: discord.ui.Button[GitHubWaitingView] = discord.ui.Button(
                    label=label, style=style
                )
                button.callback = callback  # type: ignore[method-assign]
                actions_row.add_item(button)
            container.add_item(actions_row)
        back: discord.ui.Button[GitHubWaitingView] = discord.ui.Button(label="◀ Back")
        back.callback = self._on_back  # type: ignore[method-assign]
        container.add_item(EmbedActionRow(back))
        self.add_item(container)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction, admin=True):
            return
        value = str((interaction.data or {}).get("values", [""])[0])
        try:
            selected_id = uuid.UUID(value)
        except ValueError:
            return
        if not any(row.id == selected_id for row in self.requests):
            return
        view = await load_waiting_view(
            self.state,
            runtime=self.runtime,
            user_id=self.allowed_user_id,
            interaction=interaction,
            selected_id=selected_id,
            page=self.page,
        )
        await self.swap_to(interaction, view)

    async def _on_previous(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, self.page - 1)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, self.page + 1)

    async def _change_page(self, interaction: discord.Interaction, page: int) -> None:
        if not await self._allowed(interaction, admin=True):
            return
        await self.swap_to(
            interaction,
            await load_waiting_view(
                self.state,
                runtime=self.runtime,
                user_id=self.allowed_user_id,
                interaction=interaction,
                page=page,
            ),
        )

    async def _on_cancel_select(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            return
        value = str((interaction.data or {}).get("values", [""])[0])
        try:
            request_id = uuid.UUID(value)
        except ValueError:
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        async with self.runtime.sessionmaker.begin() as session:
            await cancel_request(
                session,
                tenant_id=tenant_id,
                request_id=request_id,
                account_id=self.state.account_id,
            )
        view = await load_waiting_view(
            self.state, runtime=self.runtime, user_id=self.allowed_user_id, interaction=interaction
        )
        await self.swap_to(interaction, view)

    async def _allowed(self, interaction: discord.Interaction, *, admin: bool = False) -> bool:
        if (
            interaction.user.id != self.allowed_user_id
            or interaction.guild_id != self.state.guild_id
        ):
            await interaction.response.send_message("This panel is private.", ephemeral=True)
            return False
        server_admin = is_guild_admin(interaction)  # pyright: ignore[reportArgumentType]
        if admin and not server_admin:
            await interaction.response.send_message("This request is unavailable.", ephemeral=True)
            return False
        if admin and self.selected_id is not None:
            row = next((item for item in self.requests if item.id == self.selected_id), None)
            member = interaction.user
            guild = interaction.guild
            if row is None or guild is None or not isinstance(member, discord.Member):
                await interaction.response.send_message(
                    "This request is unavailable.", ephemeral=True
                )
                return False
            if not await _may_manage_request(
                self.runtime, row=row, member=member, is_admin=server_admin
            ):
                await interaction.response.send_message(
                    "This request is unavailable.", ephemeral=True
                )
                return False
            if not server_admin and not all(
                name.casefold()
                in {
                    repo.repo_full_name.casefold()
                    for repo in self._repos
                    if repo.status == "active"
                }
                for name in row.repo_names
            ):
                await interaction.response.send_message(
                    "Only a server admin can connect repos.", ephemeral=True
                )
                return False
            if row.parent_channel_id != row.thread_id:
                try:
                    channel = guild.get_channel(int(row.parent_channel_id))
                    if channel is None:
                        channel = await guild.fetch_channel(int(row.parent_channel_id))
                    if not channel.permissions_for(member).view_channel:
                        raise ValueError("channel unavailable")
                except (ValueError, discord.HTTPException):
                    await interaction.response.send_message(
                        "This request is unavailable.", ephemeral=True
                    )
                    return False
        return True

    async def _on_approve(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction, admin=True) or self.selected_id is None:
            return
        request = next((row for row in self.requests if row.id == self.selected_id), None)
        if request is None:
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        connected = {repo.repo_full_name.casefold() for repo in self._repos}
        try:
            async with self.runtime.sessionmaker.begin() as session:
                if all(name.casefold() in connected for name in request.repo_names):
                    changed = await approve_connected_request(
                        session,
                        tenant_id=tenant_id,
                        request_id=request.id,
                        account_id=self.state.account_id,
                    )
                    url = None
                else:
                    url = await connect_link(
                        session,
                        settings=self.runtime.settings,
                        tenant_id=tenant_id,
                        platform="discord",
                        platform_user_id=str(interaction.user.id),
                        verified_tenant_admin=is_guild_admin(interaction),  # pyright: ignore[reportArgumentType]
                        workspace_label=interaction.guild.name if interaction.guild else None,
                        requester_label=interaction.user.display_name,
                        preselected_repo_full_names=request.repo_names,
                    )
                    changed = await approve_connection_request(
                        session,
                        tenant_id=tenant_id,
                        request_id=request.id,
                        account_id=self.state.account_id,
                    )
        except ValueError as error:
            await interaction.response.send_message(safe_github_error(error), ephemeral=True)
            return
        if changed:
            from daimon.adapters.discord.agent_setup.github_requests import update_requester_card

            personal_url: str | None = None
            if url is None and self.runtime.settings.mcp.app_root_url is not None:
                async with self.runtime.sessionmaker() as session:
                    linked = await account_link_status(
                        session, account_id=request.requester_account_id
                    )
                if not linked:
                    async with self.runtime.sessionmaker.begin() as session:
                        personal_url = await mint_link(
                            session,
                            tenant_id=tenant_id,
                            account_id=request.requester_account_id,
                            platform="discord",
                            platform_user_id=request.requester_platform_user_id,
                            root_url=str(self.runtime.settings.mcp.app_root_url),
                        )
            await update_requester_card(
                interaction.client,
                self.runtime,
                request.id,
                text=(
                    "Waiting for GitHub confirmation."
                    if url is not None
                    else (
                        "Link GitHub so Daimon can check that you have access to "
                        "what this request needs."
                    )
                    if personal_url is not None
                    else f"✓ Added. {request.agent_name} is continuing."
                ),
                can_cancel=url is not None or personal_url is not None,
                link_url=personal_url,
            )
        refreshed = await load_waiting_view(
            self.state, runtime=self.runtime, user_id=self.allowed_user_id, interaction=interaction
        )
        await self.swap_to(interaction, refreshed)
        if url is not None:
            link_view = discord.ui.View(timeout=600)
            link_view.add_item(discord.ui.Button(label="Open GitHub ↗", url=url))
            await interaction.followup.send(
                "Waiting for GitHub confirmation.", view=link_view, ephemeral=True
            )

    async def _on_decline(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction, admin=True) or self.selected_id is None:
            return
        request = next((row for row in self.requests if row.id == self.selected_id), None)
        if request is None:
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        async with self.runtime.sessionmaker.begin() as session:
            changed = await set_status(
                session,
                tenant_id=tenant_id,
                request_id=request.id,
                expected=request.status,
                status="declined",
            )
        if changed:
            from daimon.adapters.discord.agent_setup.github_requests import update_requester_card

            await update_requester_card(
                interaction.client,
                self.runtime,
                request.id,
                text="An admin declined GitHub access for this request.",
                can_cancel=True,
            )
        refreshed = await load_waiting_view(
            self.state, runtime=self.runtime, user_id=self.allowed_user_id, interaction=interaction
        )
        await self.swap_to(interaction, refreshed)

    async def _on_hide(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction, admin=True) or self.selected_id is None:
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        async with self.runtime.sessionmaker.begin() as session:
            await dismiss_delivery(
                session,
                tenant_id=tenant_id,
                request_id=self.selected_id,
                account_id=self.state.account_id,
            )
        refreshed = await load_waiting_view(
            self.state, runtime=self.runtime, user_id=self.allowed_user_id, interaction=interaction
        )
        await self.swap_to(interaction, refreshed)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            return
        if self.selected_id is not None:
            view = await load_waiting_view(
                self.state,
                runtime=self.runtime,
                user_id=self.allowed_user_id,
                interaction=interaction,
            )
        else:
            from daimon.adapters.discord.agent_setup.github_home import load_home

            view = await load_home(self.state, runtime=self.runtime, user_id=self.allowed_user_id)
        await self.swap_to(interaction, view)


async def load_waiting_view(
    state: PanelState,
    *,
    runtime: DiscordRuntime,
    user_id: int,
    interaction: discord.Interaction | None = None,
    selected_id: uuid.UUID | None = None,
    page: int = 0,
) -> GitHubWaitingView:
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id))
    visible_agents = (
        frozenset(
            derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.ma_agent_id)
            for agent in state.roster_agents
        )
        if state.can_manage_github_agents
        else frozenset[uuid.UUID]()
    )
    async with runtime.sessionmaker() as session:
        requests = await list_waiting(
            session, tenant_id=tenant_id, visible_agent_ids=visible_agents
        )
        own = await list_asker_requests(session, tenant_id=tenant_id, account_id=state.account_id)
        repos = tuple(await list_authorized_repos(session, tenant_id=tenant_id))
    # A server admin may manage an agent without being able to read every
    # private channel where that agent runs. Do not reveal the channel or asker.
    guild = interaction.guild if interaction is not None else None
    member = interaction.user if interaction is not None else None
    visible_requests: list[AccessRequest] = []
    if guild is not None and isinstance(member, discord.Member):
        for row in requests:
            if not state.is_admin:
                if row.parent_channel_id == row.thread_id:
                    continue
                if not all(
                    name.casefold()
                    in {repo.repo_full_name.casefold() for repo in repos if repo.status == "active"}
                    for name in row.repo_names
                ):
                    continue
                if not await _may_manage_request(runtime, row=row, member=member, is_admin=False):
                    continue
            if row.parent_channel_id == row.thread_id:
                visible_requests.append(row)
                continue
            try:
                channel = guild.get_channel(int(row.parent_channel_id))
                if channel is None:
                    channel = await guild.fetch_channel(int(row.parent_channel_id))
                if channel.permissions_for(member).view_channel:
                    visible_requests.append(row)
            except (ValueError, discord.HTTPException):
                continue
    requester_labels: dict[str, str] = {}
    if guild is not None:
        for row in visible_requests:
            label = "a member"
            if row.requester_platform_user_id.isdigit():
                asker = guild.get_member(int(row.requester_platform_user_id))
                if asker is not None:
                    label = asker.display_name
            requester_labels[row.requester_platform_user_id] = label
    view = GitHubWaitingView(
        state,
        runtime=runtime,
        allowed_user_id=user_id,
        requests=tuple(visible_requests),
        own_requests=tuple(own),
        repos=repos,
        selected_id=selected_id,
        page=page,
        requester_labels=requester_labels,
        thread_labels=(
            {
                row.thread_id: (
                    thread.name
                    if (thread := guild.get_thread(int(row.thread_id))) is not None
                    else "This thread"
                )
                for row in own
                if row.thread_id.isdigit()
            }
            if guild is not None
            else {}
        ),
    )
    return view
