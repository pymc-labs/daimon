"""EditView (LayoutView) + selects + BackButton + open_edit_view launcher.

EditView's container holds a header, two remove selects (skills, MCP servers), and
two button rows: ``Prompt & model`` / ``Working repo`` / ``Install App…`` / ``Keys``
and ``+ Add skill`` / ``+ Add MCP server`` / ``← Back``. ``Prompt & model`` and ``Working repo``
each open their modal directly — no intermediate view. ``Install App…`` is a
link button (rendered only when a GitHub App slug is configured) opening the
App's install page on GitHub directly; it carries no callback.
"""

from __future__ import annotations

import structlog
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.adapters.discord.agent_setup import authz
from daimon.adapters.discord.agent_setup.expiry import ExpiringView
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
from daimon.core.constants import AGENT_MCP_CAP, AGENT_SKILL_CAP
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.github_app_auth import build_app_install_url
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.agent_files import list_agent_files

import discord

log = structlog.get_logger()


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
    """Swaps the root panel back onto this message; it no longer closes anything."""

    def __init__(
        self,
        *,
        state: PanelState,
        runtime: DiscordRuntime,
        allowed_user_id: int,
    ) -> None:
        super().__init__(label="← Back", style=discord.ButtonStyle.secondary)
        self.state = state
        self.runtime = runtime
        self.allowed_user_id = allowed_user_id

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        log.info("agent_setup.back_btn.click")
        await interaction.response.defer()
        try:
            # Lazy import: panel.py imports EditView/BackButton from this module
            # at module scope, so a module-scope import back would be circular.
            from daimon.adapters.discord.agent_setup.panel import rerender_root_panel

            await rerender_root_panel(
                interaction,
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
            )
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


class _SkillRemoveSelect(discord.ui.Select["EditView"]):
    """Select: pick a skill to remove (pick-to-remove, no confirm)."""

    def __init__(self, state: PanelState, *, disabled: bool = False) -> None:
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
            for idx, skill in enumerate(skills[:AGENT_SKILL_CAP])
        ]
        super().__init__(
            placeholder="✕ Remove a skill…",
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if self.view is None or self.values[0] == "__none__":
            return
        if await authz.refuse_if_reachable_and_not_admin(
            interaction, runtime=self.view.runtime, entry=self.view.state.selected
        ):
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
                ).bind_render_interaction(interaction, panel=self.view.state)
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

    def __init__(
        self, state: PanelState, *, public_url: str | None, disabled: bool = False
    ) -> None:
        mcps = (state.selected.spec.mcp_servers if state.selected is not None else None) or []
        options: list[discord.SelectOption] = []
        for idx, entry in enumerate(mcps):
            if _is_default_mcp(entry, public_url):
                continue
            if len(options) >= AGENT_MCP_CAP:
                break
            options.append(
                discord.SelectOption(label=f"✕ {entry.get('name', '?')}"[:100], value=str(idx))
            )
        if not options:
            super().__init__(
                placeholder="(no MCP servers — use + Add MCP server)",
                min_values=1,
                max_values=1,
                options=[discord.SelectOption(label="(no MCP servers)", value="__none__")],
                disabled=True,
            )
            return
        super().__init__(
            placeholder="✕ Remove an MCP…",
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if self.view is None or self.values[0] == "__none__":
            return
        if await authz.refuse_if_reachable_and_not_admin(
            interaction, runtime=self.view.runtime, entry=self.view.state.selected
        ):
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
            ).bind_render_interaction(interaction, panel=self.view.state)
        )


class EditView(ExpiringView, discord.ui.LayoutView):
    """F5 Components V2 edit view.

    Container with ## ✏️ Editing {agent} header, two remove selects, and two
    button rows: Prompt & model · Working repo · Keys, then + Add skill · + Add MCP server ·
    ← Back. Prompt & model and Working repo each open their modal directly.

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

        skill_count = len(state.selected.spec.skills) if state.selected is not None else 0
        user_mcp_count = sum(
            1
            for e in (state.selected.spec.mcp_servers if state.selected else None) or []
            if not _is_default_mcp(e, public_url)
        )

        # is_system gates the spec controls unconditionally — the seeded
        # agent's prompt/model/skills/mcp_servers stay panel-un-editable for
        # everyone, admin included (a panel edit never stamps
        # daimon_spec_hash, so reconcile would skip the drift forever).
        # spec_editable gates the same controls on reachability instead: an
        # agent nobody has scoped is every member's scratchpad; once some
        # channel or the workspace points at it, only an admin may change its
        # spec. Keys and Working repo are per-agent attachments: they never
        # enter the agent spec, so the is_system absolutism above does not
        # apply to them — an admin binding a repo to the built-in agent is a
        # supported first-run step. They are subject to their own shared-state
        # guard instead, which refuses a non-admin whenever the target is a
        # system agent or currently reachable. That is why their buttons render
        # enabled while the spec controls render disabled: the refusal happens
        # on click, not at render.
        is_system = bool(state.selected and state.selected.is_system)
        spec_editable = state.is_admin or not state.is_selected_reachable()
        spec_controls_disabled = is_system or not spec_editable

        # Button row 1: Prompt & model · Working repo · Keys.
        field_row: discord.ui.ActionRow[EditView] = discord.ui.ActionRow()

        agent_btn: discord.ui.Button[EditView] = discord.ui.Button(
            label="Prompt & model",
            style=discord.ButtonStyle.secondary,
            disabled=spec_controls_disabled,
        )
        agent_btn.callback = self._on_agent  # type: ignore[method-assign]
        field_row.add_item(agent_btn)

        github_btn: discord.ui.Button[EditView] = discord.ui.Button(
            label="Working repo",
            style=discord.ButtonStyle.secondary,
        )
        github_btn.callback = self._on_github  # type: ignore[method-assign]
        field_row.add_item(github_btn)

        # Rendered only when a GitHub App slug is configured — a link button
        # with no url is not constructible, and a disabled placeholder would
        # advertise a capability this deployment does not have.
        app_slug = runtime.settings.github.app_slug
        if app_slug is not None:
            install_app_btn: discord.ui.Button[EditView] = discord.ui.Button(
                label="🔗 Install App…",
                style=discord.ButtonStyle.link,
                url=build_app_install_url(app_slug),
            )
            # Not a spec control and no disabled logic: installing an App on
            # GitHub is neither an agent-spec edit nor an attachment write,
            # and GitHub enforces its own install permissions — same reason
            # the Working repo and Keys controls above are exempt from the gate.
            field_row.add_item(install_app_btn)

        env_vars_btn: discord.ui.Button[EditView] = discord.ui.Button(
            label="Keys",
            style=discord.ButtonStyle.secondary,
        )
        env_vars_btn.callback = self._on_env_vars  # type: ignore[method-assign]
        field_row.add_item(env_vars_btn)

        container.add_item(field_row)

        # Button row 2: + Add skill · + Add MCP server · ← Back.
        btn_row: discord.ui.ActionRow[EditView] = discord.ui.ActionRow()

        add_skill_btn: discord.ui.Button[EditView] = discord.ui.Button(
            label="+ Add skill",
            style=discord.ButtonStyle.success,
            disabled=(skill_count >= AGENT_SKILL_CAP) or spec_controls_disabled,
        )
        add_skill_btn.callback = self._on_add_skill  # type: ignore[method-assign]
        btn_row.add_item(add_skill_btn)

        add_mcp_btn: discord.ui.Button[EditView] = discord.ui.Button(
            label="+ Add MCP server",
            style=discord.ButtonStyle.success,
            disabled=(user_mcp_count >= AGENT_MCP_CAP) or spec_controls_disabled,
        )
        add_mcp_btn.callback = self._on_add_mcp  # type: ignore[method-assign]
        btn_row.add_item(add_mcp_btn)

        back_btn: discord.ui.Button[EditView] = discord.ui.Button(
            label="← Back",
            style=discord.ButtonStyle.secondary,
        )
        back_btn.callback = self._on_back  # type: ignore[method-assign]
        btn_row.add_item(back_btn)

        container.add_item(btn_row)

        # Select row 3: skill remove; row 4: MCP remove.
        skill_row: discord.ui.ActionRow[EditView] = discord.ui.ActionRow()
        skill_row.add_item(_SkillRemoveSelect(state, disabled=spec_controls_disabled))
        container.add_item(skill_row)

        mcp_row: discord.ui.ActionRow[EditView] = discord.ui.ActionRow()
        mcp_row.add_item(
            _McpRemoveSelect(state, public_url=public_url, disabled=spec_controls_disabled)
        )
        container.add_item(mcp_row)

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

    async def _on_agent(self, interaction: discord.Interaction) -> None:
        log.info("agent_setup.edit.agent.click", agent_name=self._selected_name())
        await interaction.response.send_modal(
            AgentSectionModal(
                self.state, runtime=self.runtime, allowed_user_id=self.allowed_user_id
            )
        )

    async def _on_github(self, interaction: discord.Interaction) -> None:
        log.info("agent_setup.edit.github.click", agent_name=self._selected_name())
        # Refuse before the modal opens so a member who may not bind this
        # agent's repo never types a GitHub token into a form that will be
        # rejected. RepoAuthModal.on_submit re-checks; that call is the
        # boundary, this one keeps the credential out of the payload.
        if await authz.refuse_if_shared_and_not_admin(
            interaction, runtime=self.runtime, entry=self.state.selected
        ):
            return
        await interaction.response.send_modal(
            RepoAuthModal(self.state, runtime=self.runtime, allowed_user_id=self.allowed_user_id)
        )

    async def _on_env_vars(self, interaction: discord.Interaction) -> None:
        # Lazy import: credentials.py imports EditView from this module, so a
        # top-level import here would be circular.
        from daimon.adapters.discord.agent_setup.credentials import CredentialsSubView

        selected = self.state.selected
        if selected is None:
            return
        log.info("agent_setup.edit.env_vars.click", agent_name=selected.name)
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
            ).bind_render_interaction(interaction, panel=self.state),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _on_back(self, interaction: discord.Interaction) -> None:
        log.info("agent_setup.back_btn.click")
        await interaction.response.defer()
        try:
            # Lazy import: panel.py imports EditView from this module at module
            # scope, so a module-scope import back would be circular.
            from daimon.adapters.discord.agent_setup.panel import rerender_root_panel

            await rerender_root_panel(
                interaction,
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
            )
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
    """Swap EditView onto the panel's own message.

    Owns the swap site for the Edit button callback.
    """
    view = EditView(state, runtime=runtime, allowed_user_id=allowed_user_id)
    await interaction.response.edit_message(
        view=view.bind_render_interaction(interaction, panel=state),
        allowed_mentions=discord.AllowedMentions.none(),
    )
