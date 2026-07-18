"""AgentSetupView (LayoutView) + F5 container builder + agent picker + lifecycle modals.

Plan 03 shipped the panel scaffold and the New/Fork/Delete/Done lifecycle.
The four edit sub-views were collapsed into a single ``EditView`` reached
from the Edit button.
``SetDefaultView`` and member read-only gating were added.
Plan 70-05 split EditView → edit_view.py, SetDefaultView → set_default.py,
and migrated the main panel to the locked F5 Components V2 card (LayoutView +
Container/Section/TextDisplay/Separator). Re-exports for backwards compatibility.
"""

from __future__ import annotations

import contextlib
import uuid

import structlog
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.adapters.discord.agent_setup.state import PanelState, RosterEntry
from daimon.adapters.discord.agent_setup.tenant import resolve_tenant_for_panel as _resolve_tenant
from daimon.adapters.discord.agent_setup.write import (
    create_blank_agent,
    delete_agent,
    fork_agent,
    load_selected_github_login,
    load_tenant_roster,
    validate_model_id,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.layout import hairline, header
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.observability import capture_exception_with_scope
from daimon.core.scope import (
    ChannelConfigRow,
    TenantConfigRow,
    _pick_agent,  # pyright: ignore[reportPrivateUsage]  # canonical cascade winner; adapter renders the result, never re-derives precedence.
)
from daimon.core.stores.agent_files import list_agent_files
from daimon.core.stores.agent_repo_binding import get_binding
from daimon.core.stores.domain import AgentRepoBindingRow

import discord

log = structlog.get_logger()

_SKILL_CAP = 20
_MCP_CAP = 20


def _capture_panel_exception(
    err: BaseException,
    *,
    tenant_id: uuid.UUID | None,
    guild_id: int | None,
    rid: str,
) -> None:
    """Bind tenant context into structlog contextvars and capture to Sentry.

    Panel handlers run OUTSIDE _handle_mention's rid bind (Pattern 4), so nothing is
    in contextvars at these sites. Bind tenant_id/guild_id/rid so the Sentry event is
    tenant-attributable, capture, then unbind so the binding does not leak past the
    handler. Additive only — never changes the surrounding swallow.
    """
    bound: dict[str, str] = {"rid": rid}
    if tenant_id is not None:
        bound["tenant_id"] = str(tenant_id)
    if guild_id is not None:
        bound["guild_id"] = str(guild_id)
    structlog.contextvars.bind_contextvars(**bound)
    try:
        capture_exception_with_scope(err)
    finally:
        structlog.contextvars.unbind_contextvars(*bound.keys())


def _format_channel_label(state: PanelState) -> str:
    """Render the cascade-ladder channel field name as ``#channel-name``."""
    if state.channel_name:
        return f"#{state.channel_name}"
    return f"#{state.channel_id}"


def _render_tier_value(
    row: ChannelConfigRow | TenantConfigRow | None,
) -> str:
    """Render a config row's agent for one cascade tier: ``⚙️ `name``` or ``_(unset)_``."""
    if row is None or not row.agent_name:
        return "_(unset)_"
    return f"⚙️ `{row.agent_name}`"


def _tier_display(value: str, *, is_winner: bool) -> str:
    """Apply the winner/dim treatment uniformly across tiers."""
    if is_winner:
        return f"{value} ← in effect here"
    # Dim non-winning tiers with italic markdown.
    return f"_{value}_"


# ---------------------------------------------------------------------------
# Re-exports for backwards compatibility (test_set_default.py imports from panel)
# ---------------------------------------------------------------------------

from daimon.adapters.discord.agent_setup.edit_view import (  # noqa: E402
    BackButton,
    EditView,
    _McpRemoveSelect,  # pyright: ignore[reportPrivateUsage]  # re-export for test backwards compat
    _ScalarFieldSelect,  # pyright: ignore[reportPrivateUsage]  # re-export for test backwards compat
    _SkillRemoveSelect,  # pyright: ignore[reportPrivateUsage]  # re-export for test backwards compat
)
from daimon.adapters.discord.agent_setup.set_default import (  # noqa: E402
    SetDefaultView,
)

__all__ = [
    "build_panel_container",
    "load_secret_count",
    "load_repo_binding",
    "AgentSetupView",
    "EditView",
    "SetDefaultView",
    "BackButton",
    "_McpRemoveSelect",
    "_ScalarFieldSelect",
    "_SkillRemoveSelect",
    "NewAgentModal",
    "ForkAgentModal",
]


def _is_default_mcp(entry: BetaManagedAgentsURLMCPServerParams, public_url: str | None) -> bool:
    """Return True if this MCP entry matches the operator's default public_url."""
    if public_url is None:
        return False
    return entry.get("url", "").rstrip("/") == public_url.rstrip("/")


def _count_user_mcps(entry: RosterEntry, default_mcp_url: str | None) -> int:
    """Count the agent's MCPs excluding the operator default (the body's user-MCP filter)."""
    return sum(1 for m in entry.spec.mcp_servers or [] if not _is_default_mcp(m, default_mcp_url))


def _build_vitals_subtext(state: PanelState) -> str:
    """One-line '-#' subtext: model · default-scope summary.

    Pure — reads only from state fields already populated at render time.
    """
    if state.selected is None:
        return ""
    model = state.selected.model

    # Determine effective default scope for the current channel.
    tenant_row, ch_rows = state.cascade_view
    current_channel = next((c for c in ch_rows if c.channel_id == str(state.channel_id)), None)
    _winner_name, winner_tier = _pick_agent(current_channel, tenant_row, state.deployment_default)
    if winner_tier == "channel":
        scope_label = f"default in {_format_channel_label(state)}"
    elif winner_tier == "tenant":
        scope_label = "default · whole server"
    elif winner_tier == "deployment":
        scope_label = "system default"
    else:
        scope_label = None

    if scope_label and _winner_name == state.selected.name:
        return f"{model} · ⭐ {scope_label}"
    return model


def _build_body_text(state: PanelState) -> str:
    """F5 body: bold emoji-labeled groups for non-empty resources; one dim hint
    line listing all missing resources; nothing when the agent is fully configured.

    Pure.
    """
    if state.selected is None:
        return ""

    selected = state.selected
    skills = selected.spec.skills
    all_mcps = selected.spec.mcp_servers or []
    user_mcps = [m for m in all_mcps if not _is_default_mcp(m, state.default_mcp_url)]

    groups: list[str] = []

    if skills:
        skill_titles = " · ".join(f"`{skill.skill_id}`" for skill in skills)
        groups.append(f"🧩 **Skills**\n{skill_titles}")

    if user_mcps:
        mcp_lines = "\n".join(
            f"**{m.get('name', '?')}** — `{m.get('url', '?')}`" for m in user_mcps
        )
        groups.append(f"🔌 **MCPs**\n{mcp_lines}")

    # Repo & auth group: only shown when at least one of repo/auth/secrets is set.
    has_repo = bool(state.bound_repo_url)
    has_auth = bool(state.github_login or state.pat_last4)
    has_secrets = state.secret_count > 0
    if has_repo or has_auth or has_secrets:
        repo_line_parts: list[str] = []
        if state.bound_repo_url:
            # Masked-link text must NOT be a URL: Discord auto-links the inner
            # URL and then fails to render the masked link, showing literal
            # [url](url). Use the owner/repo path as the visible label instead.
            repo_label = state.bound_repo_url.removeprefix("https://github.com/")
            repo_line_parts.append(f"[{repo_label}]({state.bound_repo_url}) `{state.bound_branch}`")
        if state.github_login:
            login_label = (
                "PAT" if state.github_login == "(inline-pat)" else f"@{state.github_login}"
            )
            repo_line_parts.append(login_label)
        elif state.pat_last4:
            repo_line_parts.append(f"PAT ••••{state.pat_last4}")
        if has_secrets:
            repo_line_parts.append(f"🔑 {state.secret_count} secrets")
        repo_line = " · ".join(repo_line_parts)
        # An anon: binding clones only when the operator fallback PAT is set.
        # inline-pat: refs always carry a per-agent PAT, so they never warn.
        wont_clone = (
            state.bound_repo_url is not None
            and state.bound_secret_ref == "anon:"
            and not state.fallback_pat_configured
        )
        if wont_clone:
            repo_line += "\n⚠️ won't clone — no token"
        if state.last_sync_error is not None:
            repo_line += f"\n⚠️ last sync failed: {state.last_sync_error}"
        groups.append(f"📦 **Repo & auth**\n{repo_line}")

    # Member read-only view: append the cascade ladder as a group.
    if not state.is_admin:
        tenant_row, ch_rows = state.cascade_view
        current_channel = next((c for c in ch_rows if c.channel_id == str(state.channel_id)), None)
        _winner_name, winner_tier = _pick_agent(
            current_channel, tenant_row, state.deployment_default
        )
        channel_label = _format_channel_label(state)

        ch_display = _tier_display(
            _render_tier_value(current_channel), is_winner=winner_tier == "channel"
        )
        ws_display = _tier_display(
            _render_tier_value(tenant_row), is_winner=winner_tier == "tenant"
        )
        sys_display = _tier_display(
            state.deployment_default.agent_name or "_(unset)_",
            is_winner=winner_tier == "deployment",
        )

        defaults_lines = (
            f"{channel_label} · {ch_display}\n"
            f"Whole server · {ws_display}\n"
            f"Deployment default · {sys_display}"
        )
        groups.append(
            f"⚙️ **Default agent**\n{defaults_lines}\n\n"
            "-# View only — ask an admin to change defaults"
        )

    # Empty resources: one shared dim hint line listing what is missing.
    missing: list[str] = []
    if not has_repo:
        missing.append("＋ repo")
    if not user_mcps:
        missing.append("＋ MCP")
    if not has_secrets:
        missing.append("＋ secrets")
    if missing and not groups and not (has_repo or has_auth or has_secrets or skills or user_mcps):
        # All resources empty — hint replaces all groups.
        hint = " · ".join(missing) + " — via **Edit**"
        return f"-# {hint}"
    if missing and groups:
        # Some resources present, some missing — append the hint after the groups.
        hint = " · ".join(missing) + " — via **Edit**"
        groups.append(f"-# {hint}")

    return "\n\n".join(groups)


def build_panel_container(
    state: PanelState,
    *,
    thumbnail_url: str | None,
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Pure: build the F5 Components V2 agent-setup card.

    Thumbnail sourcing (per plan discretion): callers pass
    ``interaction.client.user.display_avatar.url`` guarded for None client user.
    Real per-agent avatars are an explicitly deferred idea.
    No accent_color — default no accent (blurple accent dies here; the exempt
    turn lifecycle uses theme.COLOR_BLURPLE).
    """
    if state.selected is None:
        if state.is_admin:
            copy = "_This server has no agents yet._ Use **New** to create the first one."
        else:
            copy = "_This server has no agents yet._\n\nView only — ask an admin to change defaults"
        return discord.ui.Container(discord.ui.TextDisplay(copy))

    selected = state.selected
    vitals = _build_vitals_subtext(state)

    # Header: Section + Thumbnail when url given; bare TextDisplay otherwise.
    # (Section.accessory is REQUIRED — reference §7.8 — so never build a Section without one.)
    if thumbnail_url is not None:
        head: (
            discord.ui.Section[discord.ui.LayoutView]
            | discord.ui.TextDisplay[discord.ui.LayoutView]
        ) = discord.ui.Section(
            header(f"🤖 {selected.name}", subtext=vitals or None),
            accessory=discord.ui.Thumbnail(media=thumbnail_url),
        )
    else:
        head = header(f"🤖 {selected.name}", subtext=vitals or None)

    body_text = _build_body_text(state)

    return discord.ui.Container(
        head,
        hairline(),
        discord.ui.TextDisplay(body_text if body_text else "_(nothing configured yet)_"),
    )


async def load_secret_count(
    runtime: DiscordRuntime, *, tenant_id: uuid.UUID, agent_name: str
) -> int:
    """Count the secrets (agent_files) pinned to the named agent.

    Returns 0 if the agent isn't found on MA yet (e.g. a just-created agent the
    roster knows about before reconcile lands)."""
    ma_agent = await find_agent_by_daimon_tag(
        runtime.anthropic, tenant_id=tenant_id, name=agent_name
    )
    if ma_agent is None:
        return 0
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))
    async with runtime.sessionmaker() as session:
        rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_id)
    return len(rows)


async def load_repo_binding(
    runtime: DiscordRuntime, *, tenant_id: uuid.UUID, entry: RosterEntry | None
) -> AgentRepoBindingRow | None:
    """Read the persisted repo binding for ``entry``'s agent, or None if unbound."""
    if entry is None or not entry.ma_agent_id:
        return None
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=entry.ma_agent_id)
    async with runtime.sessionmaker() as session:
        return await get_binding(session, tenant_id=tenant_id, agent_id=agent_id)


class _AgentPicker(discord.ui.Select["AgentSetupView"]):
    def __init__(self, state: PanelState) -> None:
        if state.roster:
            default_mcp_url = state.default_mcp_url
            options = [
                discord.SelectOption(
                    label=entry.name,
                    value=entry.name,
                    # Enriched description: model · N skills · N MCP (default-MCP filtered)
                    description=(
                        f"{entry.model} · "
                        f"{len(entry.spec.skills)} skills · "
                        f"{_count_user_mcps(entry, default_mcp_url)} MCP"
                    )[:100],
                    default=(state.selected is not None and entry.name == state.selected.name),
                    # ⭐ default agent / 🔒 system agent
                    emoji="🔒" if entry.is_system else None,
                )
                for entry in state.roster[:25]
            ]
            disabled = False
        else:
            options = [discord.SelectOption(label="(roster empty)", value="__none__")]
            disabled = True
        super().__init__(
            placeholder="Switch agent…",
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        chosen = self.values[0]
        log.info("agent_setup.picker.selected", chosen=chosen)
        if chosen == "__none__" or self.view is None:
            return
        tenant_id: uuid.UUID | None = None
        try:
            self.view.state.select(chosen)
            # Both Secrets count and GitHub linkage live per-agent — re-fetch both.
            tenant_id = await _resolve_tenant(self.view.runtime, interaction)
            self.view.state.secret_count = await load_secret_count(
                self.view.runtime, tenant_id=tenant_id, agent_name=chosen
            )
            self.view.state.github_login = await load_selected_github_login(
                self.view.runtime, tenant_id=tenant_id, entry=self.view.state.selected
            )
            self.view.state.hydrate_repo_binding(
                await load_repo_binding(
                    self.view.runtime, tenant_id=tenant_id, entry=self.view.state.selected
                )
            )
            self.view.state.fallback_pat_configured = (
                self.view.runtime.settings.github.fallback_pat is not None
            )
            thumbnail_url = _get_thumbnail_url(interaction)
            await interaction.response.edit_message(
                view=AgentSetupView(
                    self.view.state,
                    runtime=self.view.runtime,
                    allowed_user_id=self.view.allowed_user_id,
                    thumbnail_url=thumbnail_url,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as err:
            rid = generate_request_id()
            log.exception(
                "agent_setup.picker.failed",
                chosen=chosen,
                err_type=type(err).__name__,
                request_id=rid,
            )
            _capture_panel_exception(
                err, tenant_id=tenant_id, guild_id=interaction.guild_id, rid=rid
            )
            # response may already be consumed by edit_message; try followup, fall back silently.
            with contextlib.suppress(Exception):
                await interaction.followup.send(
                    render_error(err, request_id=rid),
                    ephemeral=True,
                )
            return
        log.info("agent_setup.picker.switched", agent_name=chosen)


def _get_thumbnail_url(interaction: discord.Interaction) -> str | None:
    """Extract the bot avatar URL for use as a panel Thumbnail accessory.

    Returns None when ``interaction.client.user`` is not yet populated (e.g. in
    tests without a live bot connection) so callers get a bare TextDisplay header
    rather than an error.
    """
    if interaction.client.user is None:
        return None
    return interaction.client.user.display_avatar.url


class AgentSetupView(discord.ui.LayoutView):
    """F5 Components V2 agent-setup card.

    Container holds header, body, picker row, and (admins only) lifecycle row.
    Member gating is structural: non-admins get a container WITHOUT the lifecycle
    row — built that way, not cleared after the fact.

    Mutation buttons only appear when state.is_admin.
    """

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        thumbnail_url: str | None = None,
    ) -> None:
        super().__init__(timeout=600)
        self.state = state
        self.runtime = runtime
        self.allowed_user_id = allowed_user_id
        self._thumbnail_url = thumbnail_url

        container = build_panel_container(state, thumbnail_url=thumbnail_url)

        # Picker row (always present).
        picker_row: discord.ui.ActionRow[AgentSetupView] = discord.ui.ActionRow()
        picker: _AgentPicker = _AgentPicker(state)
        picker_row.add_item(picker)
        container.add_item(picker_row)

        if state.is_admin:
            # Lifecycle row: New / Fork / Edit / Set as default… / Delete.
            selected = state.selected
            has_selection = selected is not None
            is_system = bool(selected and selected.is_system)

            lifecycle_row: discord.ui.ActionRow[AgentSetupView] = discord.ui.ActionRow()

            new_btn: discord.ui.Button[AgentSetupView] = discord.ui.Button(
                label="New", style=discord.ButtonStyle.success
            )
            new_btn.callback = self._on_new  # type: ignore[method-assign]

            fork_btn: discord.ui.Button[AgentSetupView] = discord.ui.Button(
                label="Fork", style=discord.ButtonStyle.success
            )
            fork_btn.callback = self._on_fork  # type: ignore[method-assign]

            self.edit_btn: discord.ui.Button[AgentSetupView] = discord.ui.Button(
                label="Edit",
                style=discord.ButtonStyle.primary,
                disabled=(not has_selection) or is_system,
            )
            self.edit_btn.callback = self._on_edit  # type: ignore[method-assign]

            self.set_default_btn: discord.ui.Button[AgentSetupView] = discord.ui.Button(
                label="Set as default…",
                style=discord.ButtonStyle.primary,
                disabled=not has_selection,
            )
            self.set_default_btn.callback = self._on_set_default  # type: ignore[method-assign]

            self.delete_btn: discord.ui.Button[AgentSetupView] = discord.ui.Button(
                label="Delete",
                style=discord.ButtonStyle.danger,
                disabled=(not has_selection) or is_system,
            )
            self.delete_btn.callback = self._on_delete  # type: ignore[method-assign]

            lifecycle_row.add_item(new_btn)
            lifecycle_row.add_item(fork_btn)
            lifecycle_row.add_item(self.edit_btn)
            lifecycle_row.add_item(self.set_default_btn)
            lifecycle_row.add_item(self.delete_btn)
            container.add_item(hairline())
            container.add_item(lifecycle_row)

            # Connect row: hooks a coding agent (Claude Code, etc.) up to the
            # selected agent over MCP. Own row — the lifecycle row is full at 5.
            connect_row: discord.ui.ActionRow[AgentSetupView] = discord.ui.ActionRow()
            self.connect_mcp_btn: discord.ui.Button[AgentSetupView] = discord.ui.Button(
                label="🔌 Connect via MCP",
                style=discord.ButtonStyle.primary,
                disabled=(not has_selection) or is_system,
            )
            self.connect_mcp_btn.callback = self._on_connect_via_mcp  # type: ignore[method-assign]
            connect_row.add_item(self.connect_mcp_btn)
            container.add_item(connect_row)

        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # base uses broader Interaction[Client] type
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                "Only the command invoker can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    async def _rerender(self, interaction: discord.Interaction) -> None:
        """Re-send the panel after a state mutation."""
        tenant_id = await _resolve_tenant(self.runtime, interaction)
        self.state.github_login = await load_selected_github_login(
            self.runtime, tenant_id=tenant_id, entry=self.state.selected
        )
        self.state.hydrate_repo_binding(
            await load_repo_binding(self.runtime, tenant_id=tenant_id, entry=self.state.selected)
        )
        self.state.fallback_pat_configured = self.runtime.settings.github.fallback_pat is not None
        thumbnail_url = _get_thumbnail_url(interaction)
        await interaction.edit_original_response(
            view=AgentSetupView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                thumbnail_url=thumbnail_url,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _selected_name(self) -> str | None:
        return self.state.selected.name if self.state.selected else None

    async def _on_new(self, interaction: discord.Interaction) -> None:
        log.info("agent_setup.new_btn.click")
        await interaction.response.send_modal(
            NewAgentModal(self.state, runtime=self.runtime, allowed_user_id=self.allowed_user_id)
        )

    async def _on_connect_via_mcp(self, interaction: discord.Interaction) -> None:
        # Lazy import: mcp_access imports from state/tenant only, but keep the
        # panel's import surface minimal and avoid any cycle through agent_setup.
        from daimon.adapters.discord.agent_setup.mcp_access import send_connect_via_mcp

        log.info("agent_setup.connect_mcp_btn.click", agent_name=self._selected_name())
        await send_connect_via_mcp(
            interaction,
            runtime=self.runtime,
            state=self.state,
            allowed_user_id=self.allowed_user_id,
        )

    async def _on_fork(self, interaction: discord.Interaction) -> None:
        log.info("agent_setup.fork_btn.click", agent_name=self._selected_name())
        if self.state.selected is None:
            await interaction.response.send_message("Select an agent to fork.", ephemeral=True)
            return
        await interaction.response.send_modal(
            ForkAgentModal(self.state, runtime=self.runtime, allowed_user_id=self.allowed_user_id)
        )

    async def _on_edit(self, interaction: discord.Interaction) -> None:
        log.info("agent_setup.edit_btn.click", agent_name=self._selected_name())
        # Lazy import: edit_view.py imports helpers from panel.py at the top level,
        # so a top-level import here would be circular (privacy cascade analog).
        from daimon.adapters.discord.agent_setup.edit_view import open_edit_view

        await open_edit_view(
            interaction,
            self.state,
            runtime=self.runtime,
            allowed_user_id=self.allowed_user_id,
        )

    async def _on_delete(self, interaction: discord.Interaction) -> None:
        target_name = self.state.selected.name if self.state.selected else None
        log.info("agent_setup.delete_btn.click", agent_name=target_name)
        if self.state.selected is None or target_name is None:
            await interaction.response.send_message("Nothing to delete.", ephemeral=True)
            return
        await interaction.response.defer()
        tenant_id: uuid.UUID | None = None
        try:
            tenant_id = await _resolve_tenant(self.runtime, interaction)
            await delete_agent(self.runtime, tenant_id=tenant_id, name=target_name)
            neighbor = self.state.drop_selected()
            self.state.secret_count = (
                await load_secret_count(self.runtime, tenant_id=tenant_id, agent_name=neighbor.name)
                if neighbor is not None
                else 0
            )
            await self._rerender(interaction)
        except Exception as err:
            rid = generate_request_id()
            log.exception(
                "agent_setup.delete.failed",
                agent_name=target_name,
                err_type=type(err).__name__,
                request_id=rid,
            )
            _capture_panel_exception(
                err, tenant_id=tenant_id, guild_id=interaction.guild_id, rid=rid
            )
            await interaction.followup.send(
                render_error(err, request_id=rid),
                ephemeral=True,
            )
            return
        log.info("agent_setup.deleted", agent_name=target_name)

    async def _on_set_default(self, interaction: discord.Interaction) -> None:
        log.info("agent_setup.set_default_btn.click", agent_name=self._selected_name())
        # Lazy import: set_default.py imports helpers from panel.py at the top level.
        from daimon.adapters.discord.agent_setup.set_default import open_set_default

        await open_set_default(
            interaction,
            self.state,
            runtime=self.runtime,
            allowed_user_id=self.allowed_user_id,
        )


class NewAgentModal(discord.ui.Modal, title="New agent"):
    """Three-field modal: name, system prompt, model. Reconciles + re-renders."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
    ) -> None:
        super().__init__()
        self.state = state
        self.runtime = runtime
        self.allowed_user_id = allowed_user_id
        self.name_in: discord.ui.TextInput[NewAgentModal] = discord.ui.TextInput(
            label="Name", max_length=64, placeholder="research-bot"
        )
        self.prompt_in: discord.ui.TextInput[NewAgentModal] = discord.ui.TextInput(
            label="System prompt",
            style=discord.TextStyle.paragraph,
            max_length=2000,
            required=False,
        )
        self.model_in: discord.ui.TextInput[NewAgentModal] = discord.ui.TextInput(
            label="Model", default="claude-sonnet-4-6", max_length=64
        )
        self.add_item(self.name_in)
        self.add_item(self.prompt_in)
        self.add_item(self.model_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_name = str(self.name_in).strip()
        model_value = str(self.model_in).strip() or "claude-sonnet-4-6"
        system_value = str(self.prompt_in).strip() or None
        log.info(
            "agent_setup.new.submit",
            new_name=new_name,
            model=model_value,
            has_system=system_value is not None,
        )
        error = validate_model_id(model_value)
        if error is not None:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.defer()
        tenant_id: uuid.UUID | None = None
        try:
            tenant_id = await _resolve_tenant(self.runtime, interaction)
            await create_blank_agent(
                self.runtime,
                tenant_id=tenant_id,
                name=new_name,
                system=system_value,
                model=model_value,
                account_id=self.state.guild_account_id,  # SC-2: stamp guild account
            )
            roster = await load_tenant_roster(
                self.runtime.anthropic,
                tenant_id=tenant_id,
            )
            self.state.roster = roster
            self.state.select(new_name)
            self.state.secret_count = 0
            self.state.github_login = None
            thumbnail_url = _get_thumbnail_url(interaction)
            await interaction.edit_original_response(
                view=AgentSetupView(
                    self.state,
                    runtime=self.runtime,
                    allowed_user_id=self.allowed_user_id,
                    thumbnail_url=thumbnail_url,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as err:
            rid = generate_request_id()
            log.exception(
                "agent_setup.new.failed",
                new_name=new_name,
                model=model_value,
                err_type=type(err).__name__,
                request_id=rid,
            )
            _capture_panel_exception(
                err, tenant_id=tenant_id, guild_id=interaction.guild_id, rid=rid
            )
            await interaction.followup.send(
                render_error(err, request_id=rid),
                ephemeral=True,
            )
            return
        log.info("agent_setup.new.created", new_name=new_name, model=model_value)


class ForkAgentModal(discord.ui.Modal, title="Fork agent"):
    """Single-field modal: new name. Deep-copies source spec, only name diverges."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
    ) -> None:
        super().__init__()
        self.state = state
        self.runtime = runtime
        self.allowed_user_id = allowed_user_id
        assert state.selected is not None, "ForkAgentModal opened with no selection"
        self._source: RosterEntry = state.selected
        self.name_in: discord.ui.TextInput[ForkAgentModal] = discord.ui.TextInput(
            label="New name",
            default=f"{self._source.name}-copy",
            max_length=64,
        )
        self.add_item(self.name_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_name = str(self.name_in).strip()
        log.info(
            "agent_setup.fork.submit",
            source_name=self._source.name,
            new_name=new_name,
        )
        await interaction.response.defer()
        tenant_id: uuid.UUID | None = None
        try:
            tenant_id = await _resolve_tenant(self.runtime, interaction)
            await fork_agent(
                self.runtime,
                tenant_id=tenant_id,
                source_spec=self._source.spec,
                new_name=new_name,
                account_id=self.state.guild_account_id,  # SC-2: stamp guild account
            )
        except DaimonError as err:
            log.info(
                "agent_setup.fork.daimon_error",
                source_name=self._source.name,
                new_name=new_name,
                err=str(err),
            )
            await interaction.followup.send(str(err), ephemeral=True)
            return
        except Exception as err:
            rid = generate_request_id()
            log.exception(
                "agent_setup.fork.failed",
                source_name=self._source.name,
                new_name=new_name,
                err_type=type(err).__name__,
                request_id=rid,
            )
            _capture_panel_exception(
                err, tenant_id=tenant_id, guild_id=interaction.guild_id, rid=rid
            )
            await interaction.followup.send(
                f"Failed to fork **{self._source.name}** → **{new_name}**: "
                f"`{type(err).__name__}: {err}`",
                ephemeral=True,
            )
            return
        try:
            roster = await load_tenant_roster(
                self.runtime.anthropic,
                tenant_id=tenant_id,
            )
            self.state.roster = roster
            self.state.select(new_name)
            self.state.secret_count = 0
            self.state.github_login = None
            thumbnail_url = _get_thumbnail_url(interaction)
            await interaction.edit_original_response(
                view=AgentSetupView(
                    self.state,
                    runtime=self.runtime,
                    allowed_user_id=self.allowed_user_id,
                    thumbnail_url=thumbnail_url,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as err:
            rid = generate_request_id()
            log.exception(
                "agent_setup.fork.refresh_failed",
                source_name=self._source.name,
                new_name=new_name,
                err_type=type(err).__name__,
                request_id=rid,
            )
            _capture_panel_exception(
                err, tenant_id=tenant_id, guild_id=interaction.guild_id, rid=rid
            )
            await interaction.followup.send(
                f"Forked **{new_name}** but failed to refresh panel: `{type(err).__name__}: {err}`",
                ephemeral=True,
            )
            return
        log.info(
            "agent_setup.forked",
            source_name=self._source.name,
            new_name=new_name,
        )
