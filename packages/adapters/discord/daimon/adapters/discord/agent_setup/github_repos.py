"""GitHub repository grants inside the Discord agent setup panel."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

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
from daimon.core.github_connect_cards import (
    AgentRepoLine,
)
from daimon.core.github_panel import (
    GrantsPanel,
    connect_link,
    sync_connect_admin,
)
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.operation_policy import TargetFacts, decide_operation
from daimon.core.roster import RosterAgent
from daimon.core.stores.accounts import get_account

import discord

PAGE_SIZE = 20


def live_repo_lines(panel: GrantsPanel) -> tuple[AgentRepoLine, ...]:
    """The repos the agent uses now, as the section lists them."""
    return tuple(
        AgentRepoLine(full_name=repo.full_name, access=repo.live_ceiling)
        for repo in panel.repos
        if repo.live_ceiling is not None
    )


def section_text(agent_name: str, panel: GrantsPanel, *, page: int = 0) -> str:
    """Plain repository list with the active working repository."""
    live = live_repo_lines(panel)
    lines = [f"## {agent_name}'s repos"]
    lines.extend(repo.full_name for repo in live)
    if not live:
        lines.append("No repos")
    lines.append(f"Working repo: {panel.working_repo}" if panel.working_repo else "No working repo")
    lines.append(
        f"Open Connect GitHub to add or remove repos and choose a working repo, or ask "
        f"{agent_name} in chat."
    )
    return "\n".join(lines)


class GitHubReposView(PanelViewBase):
    """Read-only repository list for one agent."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        agent: RosterAgent,
        panel: GrantsPanel,
        connect_url: str | None = None,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        self.agent = agent
        self.panel = panel
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(section_text(agent.name, panel)))
        actions: EmbedActionRow = EmbedActionRow()
        if connect_url is not None:
            from daimon.adapters.discord.agent_setup.github_home import connect_button

            actions.add_item(connect_button(connect_url))
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

    async def _on_back(self, interaction: discord.Interaction) -> None:
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


async def mint_agent_connect_url(
    interaction: discord.Interaction,
    *,
    runtime: DiscordRuntime,
    state: PanelState,
    agent: RosterAgent,
) -> str:
    """Mint a private browser link after live manager authorization."""
    admin = is_guild_admin(interaction)  # pyright: ignore[reportArgumentType]
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id))
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.ma_agent_id)
    async with runtime.sessionmaker.begin() as session:
        if admin:
            await sync_connect_admin(
                session,
                tenant_id=tenant_id,
                platform="discord",
                platform_user_id=str(interaction.user.id),
                verified_tenant_admin=True,
            )
        return await connect_link(
            session,
            settings=runtime.settings,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id=str(interaction.user.id),
            verified_tenant_admin=admin,
            verified_agent_manager=True,
            workspace_label=interaction.guild.name if interaction.guild else None,
            requester_label=interaction.user.display_name,
            agent_id=agent_id,
            agent_name=agent.name,
            agent_ma_id=agent.ma_agent_id,
            origin_parent_channel_id=str(interaction.channel_id),
            origin_thread_id=str(interaction.channel_id),
            origin_followup_token=f"{interaction.application_id}:{interaction.token}",
            origin_followup_expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
