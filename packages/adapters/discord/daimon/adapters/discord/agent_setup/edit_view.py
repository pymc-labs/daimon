"""EditView (LayoutView) + selects + BackButton + open_edit_view launcher.

Moved verbatim from panel.py.
V2 migration: classic View → LayoutView, Auth… merge.

The Connect-GitHub (OAuth) button was removed — the Auth… button
now opens a single-option ephemeral follow-up (Paste a PAT…) that writes the
per-agent github_credentials slot.
"""

from __future__ import annotations

import structlog
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.adapters.discord.agent_setup.modals import (
    AddMcpModal,
    AddSkillModal,
    AgentSectionModal,
    RepoAuthModal,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.agent_setup.tenant import resolve_tenant_for_panel as _resolve_tenant
from daimon.adapters.discord.agent_setup.write import (
    replace_agent_resources_for_panel,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.layout import hairline, header
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.agent_files import list_agent_files

import discord

log = structlog.get_logger()

_SKILL_CAP = 20
_MCP_CAP = 20


def _is_default_mcp(entry: BetaManagedAgentsURLMCPServerParams, public_url: str | None) -> bool:
    """Return True if this MCP entry matches the operator's default public_url."""
    if public_url is None:
        return False
    return entry.get("url", "").rstrip("/") == public_url.rstrip("/")


def build_edit_container(*, agent_name: str) -> discord.ui.Container[discord.ui.LayoutView]:
    """Pure: build the EditView header container.

    Returns a Container with the ## ✏️ Editing {agent_name} header and a hairline.
    Controls (selects and buttons) are added by EditView.__init__.
    """
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
    container.add_item(header(f"✏️ Editing {agent_name}", subtext="changes apply immediately"))
    container.add_item(hairline())
    return container


class BackButton(discord.ui.Button[discord.ui.LayoutView]):
    """UX-25-01: closes the ephemeral sub-view so the main panel is visible again."""

    def __init__(self) -> None:
        super().__init__(label="← Back", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        log.info("agent_setup.back_btn.click")
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except Exception as err:
            rid = generate_request_id()
            log.exception(
                "agent_setup.back_btn.failed",
                err_type=type(err).__name__,
                request_id=rid,
            )
            await interaction.followup.send(
                render_error(err, request_id=rid),
                ephemeral=True,
            )


class _ScalarFieldSelect(discord.ui.Select["EditView"]):
    """Select: pick a scalar field (Agent / Repo) to open its modal."""

    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="✎ Agent · name / prompt / model", value="agent"),
            discord.SelectOption(label="✎ Repo · URL + branch", value="repo"),
        ]
        super().__init__(
            placeholder="Edit a field…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if self.view is None:
            return
        chosen = self.values[0]
        log.info("agent_setup.edit.scalar.pick", chosen=chosen)
        if chosen == "agent":
            await interaction.response.send_modal(
                AgentSectionModal(
                    self.view.state,
                    runtime=self.view.runtime,
                    allowed_user_id=self.view.allowed_user_id,
                )
            )
        else:
            # "repo" opens the combined RepoAuthModal.
            await interaction.response.send_modal(
                RepoAuthModal(
                    self.view.state,
                    runtime=self.view.runtime,
                    allowed_user_id=self.view.allowed_user_id,
                )
            )


class _SkillRemoveSelect(discord.ui.Select["EditView"]):
    """Select: pick a skill to remove (pick-to-remove, no confirm)."""

    def __init__(self, state: PanelState) -> None:
        skills = state.selected.spec.skills if state.selected is not None else []
        if len(skills) == 0:
            super().__init__(
                placeholder="(no skills — use + Add skill)",
                min_values=1,
                max_values=1,
                options=[discord.SelectOption(label="(no skills)", value="__none__")],
                disabled=True,
            )
            return
        options = [
            discord.SelectOption(label=f"✕ {skill.skill_id}"[:100], value=str(idx))
            for idx, skill in enumerate(skills[:_SKILL_CAP])
        ]
        super().__init__(
            placeholder="✕ Remove a skill…",
            min_values=1,
            max_values=1,
            options=options,
            disabled=False,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if self.view is None or self.values[0] == "__none__":
            return
        await interaction.response.defer()
        index = int(self.values[0])
        agent_name = self.view.state.selected.name if self.view.state.selected else None
        log.info("agent_setup.edit.skill.remove.pick", index=index, agent_name=agent_name)
        # Snapshot before mutating so we can roll back if reconcile fails.
        old_selected = self.view.state.selected
        try:
            tenant_id = await _resolve_tenant(self.view.runtime, interaction)
            self.view.state.remove_skill_at(index)
            await replace_agent_resources_for_panel(
                self.view.runtime, self.view.state, tenant_id=tenant_id
            )
            await interaction.edit_original_response(
                view=EditView(
                    self.view.state,
                    runtime=self.view.runtime,
                    allowed_user_id=self.view.allowed_user_id,
                )
            )
        except Exception as err:
            rid = generate_request_id()
            log.exception(
                "agent_setup.edit.skill.remove.failed",
                index=index,
                agent_name=agent_name,
                err_type=type(err).__name__,
                request_id=rid,
            )
            if old_selected is not None:
                self.view.state.selected = old_selected
                for idx, entry in enumerate(self.view.state.roster):
                    if entry.name == old_selected.name:
                        self.view.state.roster[idx] = old_selected
                        break
            await interaction.followup.send(
                render_error(err, request_id=rid),
                ephemeral=True,
            )
            return
        log.info("agent_setup.edit.skill.removed", index=index, agent_name=agent_name)


class _McpRemoveSelect(discord.ui.Select["EditView"]):
    """Select: pick a user MCP to remove. Default MCP filtered out.

    Each option's ``value`` carries the ORIGINAL ``mcp_servers`` index (as a
    string) — NOT the user-visible position — because ``remove_mcp_at`` indexes
    into the full unfiltered list.
    """

    def __init__(self, state: PanelState, *, public_url: str | None) -> None:
        mcps = (state.selected.spec.mcp_servers if state.selected is not None else None) or []
        options: list[discord.SelectOption] = []
        for idx, entry in enumerate(mcps):
            if _is_default_mcp(entry, public_url):
                continue
            if len(options) >= _MCP_CAP:
                break
            options.append(
                discord.SelectOption(label=f"✕ {entry.get('name', '?')}"[:100], value=str(idx))
            )
        if not options:
            super().__init__(
                placeholder="(no MCPs — use + Add MCP)",
                min_values=1,
                max_values=1,
                options=[discord.SelectOption(label="(no MCPs)", value="__none__")],
                disabled=True,
            )
            return
        super().__init__(
            placeholder="✕ Remove an MCP…",
            min_values=1,
            max_values=1,
            options=options,
            disabled=False,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if self.view is None or self.values[0] == "__none__":
            return
        await interaction.response.defer()
        # Snapshot the selected RosterEntry BEFORE the in-memory mutation —
        # remove_mcp_at replaces state.selected with a new RosterEntry. If
        # reconcile then fails, restore the snapshot so the panel doesn't lie
        # about MA state.
        old_selected = self.view.state.selected
        agent_name = old_selected.name if old_selected else None
        removed_name = self.view.state.remove_mcp_at(int(self.values[0]))
        try:
            tenant_id = await _resolve_tenant(self.view.runtime, interaction)
            outcome = await replace_agent_resources_for_panel(
                self.view.runtime, self.view.state, tenant_id=tenant_id
            )
        except Exception as err:
            rid = generate_request_id()
            log.exception(
                "mcp_remove.failed",
                mcp_name=removed_name,
                agent_name=agent_name,
                err_type=type(err).__name__,
                request_id=rid,
            )
            if old_selected is not None:
                self.view.state.selected = old_selected
                for idx, entry in enumerate(self.view.state.roster):
                    if entry.name == old_selected.name:
                        self.view.state.roster[idx] = old_selected
                        break
            await interaction.followup.send(
                render_error(err, request_id=rid),
                ephemeral=True,
            )
            return
        log.info(
            "mcp_remove.reconciled",
            mcp_name=removed_name,
            agent_name=agent_name,
            action=outcome.action.value,
            anthropic_id=outcome.anthropic_id,
        )
        await interaction.edit_original_response(
            view=EditView(
                self.view.state,
                runtime=self.view.runtime,
                allowed_user_id=self.view.allowed_user_id,
            )
        )


class _AuthFollowUpView(discord.ui.LayoutView):
    """Ephemeral follow-up for the Auth… button: Paste a PAT… (modal).

    The Connect-GitHub (OAuth) option was removed; this now
    writes the per-agent github_credentials slot via the PAT modal
    only.
    """

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self._state = state
        self._runtime = runtime
        self._allowed_user_id = allowed_user_id

        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        container.add_item(
            header(
                "🔗 GitHub auth",
                subtext="both options fill the same per-agent slot",
            )
        )

        btn_row: discord.ui.ActionRow[_AuthFollowUpView] = discord.ui.ActionRow()

        pat_btn: discord.ui.Button[_AuthFollowUpView] = discord.ui.Button(
            label="Paste a PAT…",
            style=discord.ButtonStyle.secondary,
        )
        pat_btn.callback = self._on_pat  # type: ignore[method-assign]
        btn_row.add_item(pat_btn)

        container.add_item(btn_row)
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self._allowed_user_id:
            await interaction.response.send_message(
                "Only the command invoker can use these buttons.", ephemeral=True
            )
            return False
        return True

    def _selected_name(self) -> str | None:
        return self._state.selected.name if self._state.selected else None

    async def _on_pat(self, interaction: discord.Interaction) -> None:
        log.info("agent_setup.edit.pat.click", agent_name=self._selected_name())
        await interaction.response.send_modal(
            RepoAuthModal(self._state, runtime=self._runtime, allowed_user_id=self._allowed_user_id)
        )


class EditView(discord.ui.LayoutView):
    """F5 Components V2 edit view.

    Container with ## ✏️ Editing {agent} header, three selects, and a button
    row: + Add skill · + Add MCP · Auth… · Secrets · ← Back.

    Auth… opens an ephemeral follow-up (Paste a PAT…) that binds a per-agent
    GitHub PAT (Connect-GitHub OAuth option removed).

    Preserves the isolation invariant: this view is ephemeral and
    never edits the main panel message. Mutations re-render this view via
    ``interaction.edit_original_response`` only.
    """

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self.state = state
        self.runtime = runtime
        self.allowed_user_id = allowed_user_id
        public_url = (
            str(runtime.settings.mcp.public_url)
            if runtime.settings.mcp.public_url is not None
            else None
        )

        agent_name = state.selected.name if state.selected is not None else "agent"
        container = build_edit_container(agent_name=agent_name)

        # Three select rows inside the container.
        scalar_row: discord.ui.ActionRow[EditView] = discord.ui.ActionRow()
        scalar_row.add_item(_ScalarFieldSelect())
        container.add_item(scalar_row)

        skill_row: discord.ui.ActionRow[EditView] = discord.ui.ActionRow()
        skill_row.add_item(_SkillRemoveSelect(state))
        container.add_item(skill_row)

        mcp_row: discord.ui.ActionRow[EditView] = discord.ui.ActionRow()
        mcp_row.add_item(_McpRemoveSelect(state, public_url=public_url))
        container.add_item(mcp_row)

        skill_count = len(state.selected.spec.skills) if state.selected is not None else 0
        user_mcp_count = sum(
            1
            for e in (state.selected.spec.mcp_servers if state.selected else None) or []
            if not _is_default_mcp(e, public_url)
        )

        # Button row 1: + Add skill · + Add MCP · Auth… · Secrets · ← Back.
        btn_row: discord.ui.ActionRow[EditView] = discord.ui.ActionRow()

        add_skill_btn: discord.ui.Button[EditView] = discord.ui.Button(
            label="+ Add skill",
            style=discord.ButtonStyle.success,
            disabled=skill_count >= _SKILL_CAP,
        )
        add_skill_btn.callback = self._on_add_skill  # type: ignore[method-assign]
        btn_row.add_item(add_skill_btn)

        add_mcp_btn: discord.ui.Button[EditView] = discord.ui.Button(
            label="+ Add MCP",
            style=discord.ButtonStyle.success,
            disabled=user_mcp_count >= _MCP_CAP,
        )
        add_mcp_btn.callback = self._on_add_mcp  # type: ignore[method-assign]
        btn_row.add_item(add_mcp_btn)

        auth_btn: discord.ui.Button[EditView] = discord.ui.Button(
            label="Auth…",
            style=discord.ButtonStyle.secondary,
        )
        auth_btn.callback = self._on_auth  # type: ignore[method-assign]
        btn_row.add_item(auth_btn)

        # Defensive read-only gate: system agents can never reach EditView (the
        # main panel disables Edit for them), so this `disabled` is belt-and-
        # braces (defensive).
        is_system = bool(state.selected and state.selected.is_system)
        secrets_btn: discord.ui.Button[EditView] = discord.ui.Button(
            label="Secrets",
            style=discord.ButtonStyle.secondary,
            disabled=is_system,
        )
        secrets_btn.callback = self._on_secrets  # type: ignore[method-assign]
        btn_row.add_item(secrets_btn)

        back_btn: discord.ui.Button[EditView] = discord.ui.Button(
            label="← Back",
            style=discord.ButtonStyle.secondary,
        )
        back_btn.callback = self._on_back  # type: ignore[method-assign]
        btn_row.add_item(back_btn)

        container.add_item(btn_row)
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # base uses broader Interaction[Client] type
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                "Only the command invoker can use these buttons.", ephemeral=True
            )
            return False
        return True

    def _selected_name(self) -> str | None:
        return self.state.selected.name if self.state.selected else None

    async def _on_add_skill(self, interaction: discord.Interaction) -> None:
        log.info("agent_setup.edit.skill_add.click", agent_name=self._selected_name())
        await interaction.response.send_modal(
            AddSkillModal(self.state, runtime=self.runtime, allowed_user_id=self.allowed_user_id)
        )

    async def _on_add_mcp(self, interaction: discord.Interaction) -> None:
        log.info("agent_setup.edit.mcp_add.click", agent_name=self._selected_name())
        await interaction.response.send_modal(
            AddMcpModal(self.state, runtime=self.runtime, allowed_user_id=self.allowed_user_id)
        )

    async def _on_auth(self, interaction: discord.Interaction) -> None:
        log.info("agent_setup.edit.auth.click", agent_name=self._selected_name())
        await interaction.response.send_message(
            view=_AuthFollowUpView(
                self.state, runtime=self.runtime, allowed_user_id=self.allowed_user_id
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _on_secrets(self, interaction: discord.Interaction) -> None:
        # Lazy import: credentials.py imports EditView from this module, so a
        # top-level import here would be circular.
        from daimon.adapters.discord.agent_setup.credentials import CredentialsSubView

        selected = self.state.selected
        if selected is None:
            return
        log.info("agent_setup.edit.secrets.click", agent_name=selected.name)
        tenant_id = await _resolve_tenant(self.runtime, interaction)
        ma_agent = await find_agent_by_daimon_tag(
            self.runtime.anthropic,
            tenant_id=tenant_id,
            name=selected.name,
        )
        if ma_agent is None:
            log.info("agent_setup.edit.secrets.agent_missing", agent_name=selected.name)
            await interaction.response.send_message(
                f"Could not find agent **{selected.name}** on MA.", ephemeral=True
            )
            return
        agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))
        async with self.runtime.sessionmaker() as session:
            rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_id)
        secret_names = [row.key for row in rows]
        await interaction.response.edit_message(
            view=CredentialsSubView(
                runtime=self.runtime,
                state=self.state,
                allowed_user_id=self.allowed_user_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                secret_names=secret_names,
                is_system=selected.is_system,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _on_back(self, interaction: discord.Interaction) -> None:
        log.info("agent_setup.back_btn.click")
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except Exception as err:
            rid = generate_request_id()
            log.exception(
                "agent_setup.back_btn.failed",
                err_type=type(err).__name__,
                request_id=rid,
            )
            await interaction.followup.send(
                render_error(err, request_id=rid),
                ephemeral=True,
            )


async def open_edit_view(
    interaction: discord.Interaction,
    state: PanelState,
    *,
    runtime: DiscordRuntime,
    allowed_user_id: int,
) -> None:
    """Send the ephemeral EditView. Owns the send site for the Edit button callback."""
    await interaction.response.send_message(
        view=EditView(state, runtime=runtime, allowed_user_id=allowed_user_id),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
