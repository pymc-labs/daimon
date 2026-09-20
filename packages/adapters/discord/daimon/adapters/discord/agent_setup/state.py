"""PanelState — per-View state for the /agent-setup panel."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Literal

from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.core.agent_detail_lists import DetailListName
from daimon.core.agent_details import AgentDetails
from daimon.core.answering_map import AnsweringMap
from daimon.core.roster import Page, RosterAgent, paginate
from daimon.core.scope import (
    ChannelConfigRow,
    DeploymentDefault,
    TenantConfigRow,
    is_agent_reachable,
    pick_agent,
)
from daimon.core.specs import AgentSpec
from daimon.core.stores.domain import AgentRepoBindingRow


@dataclasses.dataclass
class RosterEntry:
    """One roster row: display name, model id, and the rebuilt AgentSpec."""

    name: str
    model: str
    spec: AgentSpec
    # MA agent id (prefixed string). Empty for not-yet-created agents (New/Fork
    # before reconcile); used to derive the per-agent uuid for credential reads.
    ma_agent_id: str = ""
    is_system: bool = False


@dataclasses.dataclass(frozen=True)
class ThreadContext:
    """Why the thread the panel was opened in has a responder of its own.

    A setup thread and a handoff thread both take the mention away from the
    parent channel's agent, but they say different things to the reader, so the
    kind survives as far as the render instead of being flattened to a boolean.
    """

    kind: Literal["setup", "handoff"]
    responder_name: str | None
    target_name: str | None


@dataclasses.dataclass
class PanelState:
    """State held for the lifetime of an /agent-setup View.

    Mutated by callbacks (select / new / fork / delete / section modals).
    Recreated from MA on every /agent-setup invocation — no DB persistence.
    """

    roster: list[RosterEntry]
    selected: RosterEntry | None
    # PERSONAL principal account — principal-scoped writes (PAT/skill-sync/MCP/audit-actor)
    account_id: uuid.UUID
    # derive_guild_account_uuid(tenant_id) — ownership STAMP for create/fork/edit
    guild_account_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    platform_principal_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    pat_last4: str | None = None
    # Persisted GitHub linkage for the selected agent, hydrated from the DB at
    # panel-open and on agent-switch (per-agent overlay scope). Display
    # only — never the token. "(inline-pat)" for token-pasted creds.
    github_login: str | None = None
    mcp_token_last4: str | None = None
    # Number of secrets (agent_files) pinned to the selected agent. Loaded by the
    # shell at panel-open and refreshed whenever the selection changes (picker /
    # delete); rendering-only — never participates in reconcile.
    secret_count: int = 0
    pending_skill_repo_urls: list[str] = dataclasses.field(default_factory=list[str])
    bound_repo_url: str | None = None
    bound_branch: str = "main"
    # ma_secret_ref of the selected agent's repo binding ("anon:" / "inline-pat:…"),
    # display-only — drives the "won't clone — no token" warning. None when unbound.
    bound_secret_ref: str | None = None
    # Whether the operator fallback PAT is configured, so an anon: binding clones.
    # Set by the hydrate callers from runtime settings; display-only.
    fallback_pat_configured: bool = False
    last_sync_error: str | None = None
    default_mcp_url: str | None = None
    is_admin: bool = False
    guild_id: int = 0
    channel_id: int = 0
    # Resolved invocation-channel name (no leading #), for the cascade-ladder field
    # label. Threaded at panel-open time from the live interaction; None if unresolved.
    channel_name: str | None = None
    # (tenant_row, channel_rows) snapshot for the cascade ladder; refreshed after each write.
    cascade_view: tuple[TenantConfigRow | None, list[ChannelConfigRow]] = dataclasses.field(
        default_factory=lambda: (None, [])
    )
    # Deployment-level default injected from the runtime (config.yaml); not from DB.
    deployment_default: DeploymentDefault = dataclasses.field(default_factory=DeploymentDefault)
    # Render generation of the single /agent-setup ephemeral. Bumped by
    # ExpiringView.bind_render_interaction on every render; a view holding a
    # stale generation is off screen and must not rewrite the message.
    render_seq: int = 0
    recent_setup_conversations: list[str] = dataclasses.field(default_factory=list[str])
    # ---- Read-only setup panel (roster / details / routing) -----------------
    # The tenant's agents as `daimon.core.roster` ordered them: whichever agent
    # answers where the panel was opened first, then case-insensitive name.
    roster_agents: tuple[RosterAgent, ...] = ()
    # The agent that answers where the panel was opened, or None when nothing
    # resolves there. Setup targets this one from the roster view.
    answering: RosterAgent | None = None
    # The agent Details and setup act on. Distinct from `answering`: opening
    # Details on another agent moves this and leaves `answering` alone.
    selected_agent: RosterAgent | None = None
    # ma_agent_id -> platform mention, only for creators that resolve to a
    # Discord principal. An agent with no entry renders no attribution line.
    attributions: dict[str, str] = dataclasses.field(default_factory=dict[str, str])
    roster_page: int = 0
    routing_page: int = 0
    expanded_detail: DetailListName | None = None
    details: AgentDetails | None = None
    answering_map: AnsweringMap | None = None
    thread_context: ThreadContext | None = None
    # The thread the panel was opened in, when it was opened in one. `channel_id`
    # holds the PARENT channel in that case, so both are needed to resolve who
    # answers for the caller exactly as a mention would.
    thread_id: str | None = None

    def add_skill_repo_pending(self, url: str) -> None:
        """Mark a skill repo as in-flight; idempotent."""
        if url not in self.pending_skill_repo_urls:
            self.pending_skill_repo_urls.append(url)

    def apply_repo_modal(self, *, url: str, branch: str, pat_last4: str | None) -> None:
        """Mutate rendering-only fields. Per LD-04-01, repo binding lives in the
        agent_repo_binding store; AgentSpec carries no repo_url field."""
        self.bound_repo_url = url
        self.bound_branch = branch
        if pat_last4 is not None:
            self.pat_last4 = pat_last4

    def hydrate_repo_binding(self, row: AgentRepoBindingRow | None) -> None:
        """Set the display-only repo fields from a persisted binding (or clear
        them when the selected agent is unbound). Called at panel-open and on
        agent-switch so the Repo field reflects the DB, not just in-View edits.

        The store persists the normalized ``owner/repo`` form; rebuild the full
        ``https://github.com/owner/repo`` URL so the embed's markdown link works
        and matches what ``apply_repo_modal`` shows on a fresh add."""
        self.bound_repo_url = f"https://github.com/{row.repo_url}" if row is not None else None
        self.bound_branch = row.default_branch if row is not None else "main"
        self.bound_secret_ref = row.ma_secret_ref if row is not None else None

    def apply_agent_modal(self, *, system: str | None, model: str) -> None:
        """Apply Agent-modal edits — name is intentionally not accepted
        (Pitfall 4: rename forbidden; use Fork+Delete)."""
        if self.selected is None:
            return
        current = self.selected.spec
        updated = current.model_copy(update={"system": system, "model": model})
        self.selected = dataclasses.replace(self.selected, model=model, spec=updated)
        # Keep the roster list pointing at the new entry too.
        for idx, entry in enumerate(self.roster):
            if entry.name == self.selected.name:
                self.roster[idx] = self.selected
                break

    def apply_mcp_modal(
        self,
        *,
        server_entry: BetaManagedAgentsURLMCPServerParams,
        token_last4: str,
    ) -> None:
        """Append an MCP server to the selected agent's spec; record token last-4.

        MA rejects an agent whose ``mcp_servers`` names are not each referenced
        by a matching ``{type: mcp_toolset, mcp_server_name: <name>, ...}`` entry
        in ``tools``. Append BOTH halves here so reconcile sees a valid spec.
        Stay pure — no I/O.
        """
        if self.selected is None:
            return
        current_mcps = list(self.selected.spec.mcp_servers or [])
        current_mcps.append(server_entry)
        current_tools: list[dict[str, Any]] = [dict(t) for t in (self.selected.spec.tools or [])]
        server_name = server_entry.get("name", "")
        already_referenced = any(
            t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == server_name
            for t in current_tools
        )
        if not already_referenced:
            current_tools.append(
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": server_name,
                    "default_config": {"permission_policy": {"type": "always_allow"}},
                }
            )
        updated = self.selected.spec.model_copy(
            update={"mcp_servers": current_mcps, "tools": current_tools}
        )
        self.selected = dataclasses.replace(self.selected, spec=updated)
        for idx, entry in enumerate(self.roster):
            if entry.name == self.selected.name:
                self.roster[idx] = self.selected
                break
        self.mcp_token_last4 = token_last4

    def remove_skill_at(self, index: int) -> None:
        """Remove the skill at `index` from the selected agent's spec."""
        if self.selected is None:
            return
        skills = list(self.selected.spec.skills)
        if 0 <= index < len(skills):
            skills.pop(index)
            updated = self.selected.spec.model_copy(update={"skills": skills})
            self.selected = dataclasses.replace(self.selected, spec=updated)
            for idx, entry in enumerate(self.roster):
                if entry.name == self.selected.name:
                    self.roster[idx] = self.selected
                    break

    def remove_mcp_at(self, index: int) -> str | None:
        """Remove the user MCP at ``index`` from the selected agent's spec.

        An ``mcp_toolset`` entry in ``tools`` that references a removed
        ``mcp_servers`` name is an MA validation error on the next reconcile.
        Remove both halves atomically.

        Returns the removed MCP's name (for logging / error surfacing), or
        ``None`` if nothing was removed (no selection, or index out of range).
        """
        if self.selected is None:
            return None
        mcps = list(self.selected.spec.mcp_servers or [])
        if not (0 <= index < len(mcps)):
            return None
        removed_entry = mcps.pop(index)
        removed_name = removed_entry.get("name", "")
        tools: list[dict[str, Any]] = [
            dict(t)
            for t in (self.selected.spec.tools or [])
            if not (t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == removed_name)
        ]
        updated = self.selected.spec.model_copy(
            update={"mcp_servers": mcps or None, "tools": tools or None}
        )
        self.selected = dataclasses.replace(self.selected, spec=updated)
        for idx, entry in enumerate(self.roster):
            if entry.name == self.selected.name:
                self.roster[idx] = self.selected
                break
        return removed_name

    def roster_page_of(self, page_size: int) -> Page[RosterAgent]:
        """The current window onto `roster_agents`, clamped into range.

        `roster_page` can outlive the rows it indexed — an agent archived in
        chat between two clicks shortens the roster — so the page number is
        clamped rather than trusted.
        """
        return paginate(self.roster_agents, page=self.roster_page, page_size=page_size)

    def select_agent(self, agent: RosterAgent) -> None:
        """Point Details and setup at `agent`, keeping the legacy selection in step.

        The editor panel still reads `selected`; while both panels exist in the
        tree, a selection made on the new one has to be visible to the old one
        or the two disagree about what setup would target.
        """
        self.selected_agent = agent
        self.expanded_detail = None
        for entry in self.roster:
            if entry.name == agent.name:
                self.selected = entry
                return

    def select(self, name: str) -> None:
        for entry in self.roster:
            if entry.name == name:
                self.selected = entry
                return

    def drop_selected(self) -> RosterEntry | None:
        """Remove the currently-selected entry; pick a neighbor if any remain.

        Returns the new selection (a neighbor, or ``None`` if the roster is now
        empty) so callers can act on the post-drop selection without re-reading
        the attribute (whose narrowing they can't see through this call)."""
        if self.selected is None:
            return None
        try:
            idx = self.roster.index(self.selected)
        except ValueError:
            self.selected = None
            return None
        self.roster.pop(idx)
        if not self.roster:
            self.selected = None
        else:
            self.selected = self.roster[min(idx, len(self.roster) - 1)]
        return self.selected

    def is_selected_reachable(self) -> bool:
        """Whether some channel or the workspace currently resolves to the
        selected agent, including through the deployment default fall-through.

        This is a distinct question from `RosterEntry.is_system`: reachability
        is a live cascade property (does anything currently point at this
        agent), while `is_system` is a provenance marker (was this agent
        created by the defaults seed). They coincide for the seeded agent on
        a fresh install, but one never implies the other.
        """
        if self.selected is None:
            return False
        tenant_row, channel_rows = self.cascade_view
        return is_agent_reachable(
            self.selected.name,
            tenant=tenant_row,
            channels=channel_rows,
            default=self.deployment_default,
        )

    @classmethod
    def initial(
        cls,
        *,
        roster: list[RosterEntry],
        account_id: uuid.UUID,
        platform_principal_id: uuid.UUID,
        guild_account_id: uuid.UUID | None = None,
        default_mcp_url: str | None = None,
        is_admin: bool = False,
        guild_id: int = 0,
        channel_id: int = 0,
        channel_name: str | None = None,
        cascade_view: tuple[TenantConfigRow | None, list[ChannelConfigRow]] | None = None,
        deployment_default: DeploymentDefault | None = None,
        secret_count: int = 0,
    ) -> PanelState:
        tenant_row, channel_rows = cascade_view if cascade_view is not None else (None, [])
        channel_row = next((row for row in channel_rows if row.channel_id == str(channel_id)), None)
        responder_name, _ = pick_agent(
            channel_row, tenant_row, deployment_default or DeploymentDefault()
        )
        state = cls(
            roster=roster,
            selected=next((entry for entry in roster if entry.name == responder_name), None),
            account_id=account_id,
            platform_principal_id=platform_principal_id,
            default_mcp_url=default_mcp_url,
            is_admin=is_admin,
            guild_id=guild_id,
            channel_id=channel_id,
            channel_name=channel_name,
            cascade_view=cascade_view if cascade_view is not None else (None, []),
            deployment_default=deployment_default
            if deployment_default is not None
            else DeploymentDefault(),
            secret_count=secret_count,
        )
        if guild_account_id is not None:
            state.guild_account_id = guild_account_id
        return state
