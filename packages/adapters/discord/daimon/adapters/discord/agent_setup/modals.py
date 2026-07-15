"""Section modals + helpers for /agent-setup (Plan 04).

Four modals are added on top of the Plan-03 lifecycle modals:

- AgentSectionModal: edit system prompt + model (name is intentionally
  not rebindable — Pitfall 4: use Fork+Delete to rename).
- RepoAuthModal: bind repo + branch; optional inline PAT path stores
  Fernet-encrypted in `github_credentials`. Per LD-04-01, the per-agent
  binding lives in `agent_repo_binding`, NOT on AgentSpec.
- AddSkillModal: kicks off Phase 33's sync_agent_skills via
  asyncio.create_task (fire-and-forget).
- AddMcpModal: appends a real BetaManagedAgentsURLMCPServerParams entry
  to the agent's spec, then reconciles.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import structlog
from daimon.adapters.discord.agent_setup.modals_mcp import AddMcpModal as AddMcpModal
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.agent_setup.tenant import resolve_tenant_for_panel
from daimon.adapters.discord.agent_setup.write import (
    call_reconcile_for_panel,
    kick_off_skill_sync,
    mask_tail,
    store_inline_pat,
    validate_model_id,
)
from daimon.adapters.discord.github_visibility import is_public_repo, pat_can_access_repo
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import DaimonError
from daimon.core.github_repo_auth import is_app_installed_for_repo
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.agent_repo_binding import set_binding as set_agent_repo_binding

import discord

_log = structlog.get_logger()


def _owner_repo_from_url(url: str) -> str:
    """Extract canonical ``owner/repo`` from a GitHub URL or short path.

    Strips a leading ``https://github.com/`` / ``github.com/`` prefix and any
    trailing ``/`` or ``.git`` suffix, keeping only the first two path segments.
    """
    stripped = url.strip()
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :]
            break
    stripped = stripped.rstrip("/")
    if stripped.endswith(".git"):
        stripped = stripped[: -len(".git")]
    parts = [p for p in stripped.split("/") if p]
    return "/".join(parts[:2])


_SYSTEM_PROMPT_MAX = 4000
"""Discord TextInput hard max. Prompts longer than this can't be prefilled, so
the Agent modal omits them rather than failing to open (preserved on submit)."""


class AgentSectionModal(discord.ui.Modal, title="Agent"):
    """Edit system prompt + model. Name field shown read-only; never rebound."""

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
        # Show the current name as a placeholder, but on_submit ignores edits.
        # Pitfall 4: forbid rename day-1.
        current = state.selected
        self.name_in: discord.ui.TextInput[AgentSectionModal] = discord.ui.TextInput(
            label="Name (read-only; Fork+Delete to rename)",
            placeholder=current.name if current is not None else "",
            required=False,
            max_length=64,
        )
        # Discord rejects a modal whose prefilled value exceeds the field's
        # max_length (hard limit 4000). Seeded prompts plus the injected
        # credential-guidance preamble can blow past 4000, which made the whole
        # Agent modal un-openable — blocking even a model-only edit. When the
        # current prompt is too long to prefill, omit it (blank field + a note)
        # and preserve it on submit so a blank does NOT wipe the stored prompt.
        current_system = (current.spec.system or "") if current is not None else ""
        self._system_omitted = len(current_system) > _SYSTEM_PROMPT_MAX
        self.prompt_in: discord.ui.TextInput[AgentSectionModal] = discord.ui.TextInput(
            label="System prompt",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=_SYSTEM_PROMPT_MAX,
            default="" if self._system_omitted else current_system,
            placeholder=(
                f"Hidden — {len(current_system)} chars exceeds the {_SYSTEM_PROMPT_MAX} "
                "limit. Blank keeps it; type to replace."
            )
            if self._system_omitted
            else "",
        )
        self.model_in: discord.ui.TextInput[AgentSectionModal] = discord.ui.TextInput(
            label="Model",
            max_length=64,
            default=current.model if current is not None else "claude-sonnet-4-6",
        )
        self.add_item(self.name_in)
        self.add_item(self.prompt_in)
        self.add_item(self.model_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # name_in is intentionally ignored — rename forbidden (Pitfall 4).
        model_value = str(self.model_in).strip() or "claude-sonnet-4-6"
        submitted_system = str(self.prompt_in).strip()
        if self._system_omitted and not submitted_system:
            # Prompt was too long to show; a blank submit must KEEP it, not wipe it.
            current = self.state.selected
            system_value = current.spec.system if current is not None else None
        else:
            system_value = submitted_system or None
        agent_name = self.state.selected.name if self.state.selected else None
        _log.info(
            "agent_setup.agent_section.submit",
            agent_name=agent_name,
            model=model_value,
            has_system=system_value is not None,
        )
        error = validate_model_id(model_value)
        if error is not None:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.defer()
        try:
            tenant_id = await resolve_tenant_for_panel(self.runtime, interaction)
            self.state.apply_agent_modal(system=system_value, model=model_value)
            outcome = await call_reconcile_for_panel(self.runtime, self.state, tenant_id=tenant_id)
            from daimon.adapters.discord.agent_setup.panel import (
                AgentSetupView,
                _get_thumbnail_url,  # pyright: ignore[reportPrivateUsage]  # module-internal helper
            )

            await interaction.edit_original_response(
                view=AgentSetupView(
                    self.state,
                    runtime=self.runtime,
                    allowed_user_id=self.allowed_user_id,
                    thumbnail_url=_get_thumbnail_url(interaction),
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as err:
            _log.exception(
                "agent_setup.agent_section.failed",
                agent_name=agent_name,
                model=model_value,
                err_type=type(err).__name__,
            )
            await interaction.followup.send(
                f"Failed to update **{agent_name}**: `{type(err).__name__}: {err}`",
                ephemeral=True,
            )
            return
        _log.info(
            "agent_setup.agent_section.reconciled",
            agent_name=agent_name,
            model=model_value,
            action=outcome.action.value,
            anthropic_id=outcome.anthropic_id,
        )


class RepoAuthModal(discord.ui.Modal, title="Repo + Auth"):
    """Bind repo URL + branch; optional inline PAT.

    Per LD-04-01, persists via `agent_repo_binding.set_binding` — AgentSpec
    does NOT carry repo_url. Per LD-04-02, modals cannot mix buttons with
    TextInputs, so the Connect-GitHub button lives on a separate panel view.
    """

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
        self.url_in: discord.ui.TextInput[RepoAuthModal] = discord.ui.TextInput(
            label="Repo URL",
            placeholder="https://github.com/org/repo",
            max_length=1024,
        )
        self.branch_in: discord.ui.TextInput[RepoAuthModal] = discord.ui.TextInput(
            label="Branch",
            default=state.bound_branch,
            max_length=255,
        )
        self.pat_in: discord.ui.TextInput[RepoAuthModal] = discord.ui.TextInput(
            label="PAT (optional)",
            required=False,
            max_length=255,
        )
        self.add_item(self.url_in)
        self.add_item(self.branch_in)
        self.add_item(self.pat_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        url = str(self.url_in).strip()
        branch = str(self.branch_in).strip() or "main"
        pat = str(self.pat_in).strip()
        agent_name = self.state.selected.name if self.state.selected else None
        _log.info(
            "agent_setup.repo_auth.submit",
            agent_name=agent_name,
            repo_url=url,
            branch=branch,
            pat_masked=mask_tail(pat) if pat else None,
        )
        await interaction.response.defer()
        try:
            tenant_id = await resolve_tenant_for_panel(self.runtime, interaction)

            # Resolve the MA agent UUID first — needed for both the per-agent
            # credential write (D-25) and the repo binding below.
            selected = self.state.selected
            if selected is None:
                return
            ma_agent = await find_agent_by_daimon_tag(
                self.runtime.anthropic,
                tenant_id=tenant_id,
                name=selected.name,
            )
            if ma_agent is None:
                _log.info(
                    "agent_setup.repo_auth.agent_missing",
                    agent_name=selected.name,
                )
                await interaction.followup.send(
                    f"Could not find agent **{selected.name}** on MA.", ephemeral=True
                )
                return
            agent_uuid = derive_agent_uuid(
                tenant_id=tenant_id,
                ma_agent_id=str(ma_agent.id),
            )

            pat_last4: str | None = None
            ma_secret_ref: str
            coverage_note: str | None = None
            if pat:
                # Verify the pasted PAT actually grants access to this repo BEFORE
                # binding. Otherwise a guild could bind a repo it does not control
                # with a junk PAT and, on the next webhook resync, ride the
                # deployment's GitHub App installation token (keyed by repo, not
                # tenant) to clone another tenant's private repo.
                owner_repo = _owner_repo_from_url(url)
                async with httpx.AsyncClient() as http_client:
                    has_access = await pat_can_access_repo(
                        http_client, owner_repo=owner_repo, pat=pat
                    )
                if not has_access:
                    raise DaimonError(
                        "That token can't access this repo (or the repo doesn't "
                        "exist). Paste a PAT that has access, or connect GitHub."
                    )
                # D-25: inline PAT is written as a per-agent credential keyed on
                # agent_uuid (not account_id). Only this agent can resolve it.
                ma_secret_ref = await store_inline_pat(
                    self.runtime,
                    account_id=self.state.account_id,
                    agent_id=agent_uuid,
                    plaintext_pat=pat,
                )
                pat_last4 = pat[-4:]
            else:
                # No inline PAT -> probe GitHub App coverage first (D-06). If the
                # App is installed on the repo owner, App mode will clone it
                # (public or private) without the operator fallback PAT, so the
                # public-only visibility check is unnecessary. A probe failure
                # must never block the bind (T-97-12) -- it degrades to the
                # existing public-repo check, same as an App-less deployment.
                owner_repo = _owner_repo_from_url(url)
                owner, repo_name = owner_repo.split("/", 1)
                app_covered = False
                async with httpx.AsyncClient() as http_client:
                    try:
                        app_covered = await is_app_installed_for_repo(
                            http_client,
                            app_id=self.runtime.settings.github.app_id,
                            app_private_key=self.runtime.settings.github.app_private_key,
                            owner=owner,
                            repo=repo_name,
                            now=int(time.time()),
                        )
                    except Exception:  # noqa: BLE001 -- T-97-12: a coverage probe is best-effort UI; ANY failure (HTTP, or a malformed App key raising from build_app_jwt) must degrade to the public-repo check, never block the bind.
                        _log.warning(
                            "agent_setup.repo_auth.app_coverage_probe_failed",
                            agent_name=agent_name,
                            repo_url=url,
                        )
                        coverage_note = "Couldn't verify App coverage."
                    if app_covered:
                        coverage_note = "✅ App-covered (clones as daimon-cma[bot])"
                    else:
                        # No App coverage (or unverifiable) -> the only remaining
                        # clone token will be the operator fallback PAT, which is
                        # public-only. Verify the repo is public BEFORE writing an
                        # anon: binding, so a private repo can never be cloned
                        # cross-tenant with the operator token.
                        public = await is_public_repo(http_client, owner_repo=owner_repo)
                        if not public:
                            raise DaimonError(
                                "This repo is private (or not found). Paste a PAT to bind it."
                            )
                # D-25: no inline PAT -> no per-agent credential is written. The resync
                # path is agent-overlay-only and never consults a principal-default, so
                # mark the ref as anonymous rather than implying a fallback exists.
                ma_secret_ref = "anon:"

            # LD-04-01: persist binding via the dedicated store.
            async with self.runtime.sessionmaker.begin() as session:
                await set_agent_repo_binding(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent_uuid,
                    repo_url=url,
                    default_branch=branch,
                    ma_secret_ref=ma_secret_ref,
                )

            self.state.apply_repo_modal(url=url, branch=branch, pat_last4=pat_last4)
            await call_reconcile_for_panel(self.runtime, self.state, tenant_id=tenant_id)

            from daimon.adapters.discord.agent_setup.panel import (
                AgentSetupView,
                _get_thumbnail_url,  # pyright: ignore[reportPrivateUsage]  # module-internal helper
            )

            await interaction.edit_original_response(
                view=AgentSetupView(
                    self.state,
                    runtime=self.runtime,
                    allowed_user_id=self.allowed_user_id,
                    thumbnail_url=_get_thumbnail_url(interaction),
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if coverage_note is not None:
                await interaction.followup.send(coverage_note, ephemeral=True)
        except Exception as err:
            _log.exception(
                "agent_setup.repo_auth.failed",
                agent_name=agent_name,
                repo_url=url,
                branch=branch,
                pat_provided=bool(pat),
                err_type=type(err).__name__,
            )
            await interaction.followup.send(
                f"Failed to bind repo for **{agent_name}**: `{type(err).__name__}: {err}`",
                ephemeral=True,
            )
            return
        _log.info(
            "agent_setup.repo_auth.bound",
            agent_name=agent_name,
            repo_url=url,
            branch=branch,
            pat_provided=bool(pat),
        )


class AddSkillModal(discord.ui.Modal, title="Add skill repo"):
    """Add one Skills repo URL. Kicks off Phase 33 sync_agent_skills async."""

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
        self.url_in: discord.ui.TextInput[AddSkillModal] = discord.ui.TextInput(
            label="Skills repo URL",
            placeholder="https://github.com/org/skills",
            max_length=1024,
        )
        self.add_item(self.url_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        url = str(self.url_in).strip()
        selected = self.state.selected
        agent_name = selected.name if selected else None
        _log.info(
            "agent_setup.skill_repo.submit",
            agent_name=agent_name,
            repo_url=url,
        )
        await interaction.response.defer()
        if selected is None or not url:
            return
        tenant_id = await resolve_tenant_for_panel(self.runtime, interaction)
        try:
            self.state.add_skill_repo_pending(url)
            from daimon.adapters.discord.agent_setup.panel import (
                AgentSetupView,
                _get_thumbnail_url,  # pyright: ignore[reportPrivateUsage]  # module-internal helper
            )

            await interaction.edit_original_response(
                view=AgentSetupView(
                    self.state,
                    runtime=self.runtime,
                    allowed_user_id=self.allowed_user_id,
                    thumbnail_url=_get_thumbnail_url(interaction),
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as err:
            _log.exception(
                "agent_setup.skill_repo.failed",
                agent_name=agent_name,
                repo_url=url,
                err_type=type(err).__name__,
            )
            await interaction.followup.send(
                f"Failed to queue skill sync for **{agent_name}**: `{type(err).__name__}: {err}`",
                ephemeral=True,
            )
            return
        _log.info(
            "agent_setup.skill_repo.queued",
            agent_name=agent_name,
            repo_url=url,
        )

        async def _run_and_toast() -> None:
            try:
                report = await kick_off_skill_sync(
                    self.runtime,
                    tenant_id=tenant_id,
                    account_id=self.state.account_id,
                    agent_name=selected.name,
                    repo_url=url,
                )
            except Exception as sync_err:
                _log.exception(
                    "agent_setup.skill_repo.sync_failed",
                    agent_name=agent_name,
                    repo_url=url,
                    err_type=type(sync_err).__name__,
                )
                await interaction.followup.send(
                    f"✗ Sync failed for **{agent_name}**: `{type(sync_err).__name__}: {sync_err}`",
                    ephemeral=True,
                )
                return
            failures = [f"{name}: {reason}" for name, reason in report.failed_uploads] + [
                f"{repo}: {reason}" for repo, reason in report.skipped_repos
            ]
            n_ok = report.synced + report.updated
            if not failures:
                content = f"✓ Synced {n_ok} skill(s) from {url}."
            elif n_ok > 0:
                content = f"⚠ Synced {n_ok} skill(s), {len(failures)} failed: " + "; ".join(
                    failures
                )
            else:
                content = "✗ Sync failed: " + "; ".join(failures)
            _log.info(
                "agent_setup.skill_repo.sync_done",
                agent_name=agent_name,
                repo_url=url,
                n_ok=n_ok,
                n_failed=len(failures),
            )
            await interaction.followup.send(content, ephemeral=True)

        asyncio.create_task(_run_and_toast())  # noqa: RUF006 — background toast; interaction token valid 15 min


# AddMcpModal lives in modals_mcp.py (LD-04-03 split — modals.py LOC budget).
# Re-exported above for backward import compatibility.
