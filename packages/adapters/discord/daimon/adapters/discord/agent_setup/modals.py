"""Section modals + helpers for /agent-setup (Plan 04).

Four modals are added on top of the Plan-03 lifecycle modals:

- AgentSectionModal: edit system prompt + model (name is intentionally
  not rebindable — Pitfall 4: use Fork+Delete to rename).
- RepoAuthModal: bind repo + branch; optional inline PAT path stores
  Fernet-encrypted in `github_credentials`. Per LD-04-01, the per-agent
  binding lives in `agent_repo_binding`, NOT on AgentSpec.
- AddSkillModal: kicks off sync_agent_skills via
  asyncio.create_task (fire-and-forget).
- AddMcpModal: appends a real BetaManagedAgentsURLMCPServerParams entry
  to the agent's spec, then reconciles.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import structlog
from daimon.adapters.discord.agent_setup import authz
from daimon.adapters.discord.agent_setup.modals_mcp import AddMcpModal as AddMcpModal
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.agent_setup.tenant import resolve_tenant_for_panel
from daimon.adapters.discord.agent_setup.write import (
    call_reconcile_for_panel,
    kick_off_skill_sync,
    load_agent_inline_pat,
    mask_tail,
    store_inline_pat,
    validate_model_id,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.constants import DEFAULT_AGENT_MODEL
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import DaimonError
from daimon.core.github_visibility import (
    is_public_repo,
    is_valid_pat,
    pat_can_access_repo,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.agent_repo_binding import set_binding as set_agent_repo_binding
from daimon.core.stores.domain import RepoAccessProof

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


class AgentSectionModal(discord.ui.Modal, title="Prompt & model"):
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
            default=current.model if current is not None else DEFAULT_AGENT_MODEL,
        )
        self.add_item(self.name_in)
        self.add_item(self.prompt_in)
        self.add_item(self.model_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # name_in is intentionally ignored — rename forbidden (Pitfall 4).
        model_value = str(self.model_in).strip() or DEFAULT_AGENT_MODEL
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
        if await authz.refuse_if_reachable_and_not_admin(
            interaction, runtime=self.runtime, entry=self.state.selected
        ):
            return
        await interaction.response.defer()
        try:
            tenant_id = await resolve_tenant_for_panel(self.runtime, interaction)
            self.state.apply_agent_modal(system=system_value, model=model_value)
            outcome = await call_reconcile_for_panel(self.runtime, self.state, tenant_id=tenant_id)
            from daimon.adapters.discord.agent_setup.edit_view import EditView

            await interaction.edit_original_response(
                view=EditView(
                    self.state,
                    runtime=self.runtime,
                    allowed_user_id=self.allowed_user_id,
                ).bind_render_interaction(interaction, panel=self.state),
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
                f"Could not confirm the update to **{agent_name}**. "
                "Open `/agent-setup` to check its prompt and model before trying again.",
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


class RepoAuthModal(discord.ui.Modal, title="Working repo"):
    """Bind a repo URL, store a GitHub token, or both.

    Repo URL is optional: a token submitted with no repo is verified against
    its own GitHub identity (`is_valid_pat`, no repo-scoped permission
    needed), stored, and writes NO `agent_repo_binding` row — the GitHub
    Copilot MCP mirror in `core/sessions.py` picks it up on the next session
    with zero core changes. A repo submitted with no token still runs the
    existing App-coverage / public-visibility probes; it ALSO re-verifies
    any already-stored inline PAT against the newly-typed repo before
    trusting it, since that stored PAT — not App/public coverage — is
    what will actually clone the repo. The repo binding persists via
    `agent_repo_binding.set_binding`, never on AgentSpec. Modals cannot mix
    buttons with TextInputs.
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
            label="Repo URL (optional)",
            placeholder="Leave blank to store only the token below",
            required=False,
            max_length=1024,
        )
        self.branch_in: discord.ui.TextInput[RepoAuthModal] = discord.ui.TextInput(
            label="Branch",
            default=state.bound_branch,
            max_length=255,
        )
        self.pat_in: discord.ui.TextInput[RepoAuthModal] = discord.ui.TextInput(
            label="Token (optional; also powers GitHub MCP)",
            placeholder="ghp_… — clones the repo and powers the GitHub MCP server",
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
        if not url and not pat:
            # Both fields blank must be refused, not written as an
            # empty binding. Before defer() so the user gets an immediate
            # validation reply.
            await interaction.response.send_message(
                "Enter a repo URL, a GitHub token, or both.", ephemeral=True
            )
            return
        # Submit time is the boundary: a modal opened before the caller lost
        # Manage Server must not write. Runs before defer() so the refusal owns
        # the first response, and before the log line below so a refused
        # submission leaves no record of the masked token or of a repo URL the
        # caller had no authority to bind.
        if await authz.refuse_if_shared_and_not_admin(
            interaction, runtime=self.runtime, entry=self.state.selected
        ):
            return
        _log.info(
            "agent_setup.repo_auth.submit",
            agent_name=agent_name,
            repo_url=url or None,
            branch=branch,
            pat_masked=mask_tail(pat) if pat else None,
        )
        await interaction.response.defer()
        try:
            tenant_id = await resolve_tenant_for_panel(self.runtime, interaction)

            # Resolve the MA agent UUID first — needed for both the per-agent
            # credential write and the repo binding below.
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

            coverage_note: str | None = None
            if not url:
                # PAT-only path: the guard above guarantees `pat` is
                # non-empty here. There's no repo to check access against yet,
                # so verify only the token's own GitHub identity, store it,
                # and write NO agent_repo_binding row — neither `inline-pat:`
                # nor `anon:` applies when there is no repo, and no third ref
                # may be invented. Nothing on the AgentSpec changed, so skip
                # reconcile too.
                async with httpx.AsyncClient() as http_client:
                    token_valid = await is_valid_pat(http_client, pat=pat)
                if not token_valid:
                    raise DaimonError(
                        "GitHub rejected that token. Paste a valid personal "
                        "access token (it must not be expired)."
                    )
                await store_inline_pat(
                    self.runtime,
                    account_id=self.state.account_id,
                    agent_id=agent_uuid,
                    plaintext_pat=pat,
                )
                self.state.pat_last4 = pat[-4:]
                coverage_note = (
                    "Token stored — no repo pinned. The GitHub MCP server "
                    "picks it up on the next session."
                )
            else:
                pat_last4: str | None = None
                ma_secret_ref: str
                proof: RepoAccessProof
                now = datetime.now(UTC)
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
                    # inline PAT is written as a per-agent credential keyed on
                    # agent_uuid (not account_id). Only this agent can resolve it.
                    ma_secret_ref = await store_inline_pat(
                        self.runtime,
                        account_id=self.state.account_id,
                        agent_id=agent_uuid,
                        plaintext_pat=pat,
                    )
                    pat_last4 = pat[-4:]
                    proof = RepoAccessProof(kind="pat", at=now, account_id=self.state.account_id)
                else:
                    owner_repo = _owner_repo_from_url(url)
                    # A blank PAT field does NOT mean "no inline PAT will
                    # clone this repo" -- an earlier repo-free submit may
                    # already have stored one for this agent. sessions.py resolves
                    # per_agent_pat via get_pat(agent_id=...) unconditionally, and
                    # select_clone_auth gives it unconditional precedence over both
                    # the App and public paths -- so that stored PAT, not App/public
                    # coverage, is what will actually clone this repo. It must be
                    # re-verified against THIS repo before the bind is allowed to
                    # succeed; falling through to the App/public probes on failure
                    # would let a "successful" bind silently produce a broken or
                    # cross-tenant clone later.
                    existing_pat = await load_agent_inline_pat(self.runtime, agent_id=agent_uuid)
                    if existing_pat is not None:
                        async with httpx.AsyncClient() as http_client:
                            covers_new_repo = await pat_can_access_repo(
                                http_client, owner_repo=owner_repo, pat=existing_pat
                            )
                        if not covers_new_repo:
                            raise DaimonError(
                                "This agent already has a stored GitHub token that "
                                "can't access this repo. Paste a token that can, or "
                                "clear the stored one, then bind again."
                            )
                        # The stored PAT covers this repo -> it (not App/public) is
                        # what will clone it, so record that ref and skip both probes.
                        ma_secret_ref = f"inline-pat:{agent_uuid}"
                        proof = RepoAccessProof(
                            kind="pat", at=now, account_id=self.state.account_id
                        )
                    else:
                        # No inline PAT -> a bind with no token can only be trusted
                        # against a repo anyone can read, because the only credential
                        # that will ever clone this binding afterward is the
                        # operator's public-read-only fallback PAT. GitHub App
                        # installation is irrelevant here: an App is installed by a
                        # repo owner for their own tenant, and its installation is
                        # keyed by repo, not by the tenant doing the binding here —
                        # so App coverage proves nothing about whether THIS binder
                        # may read the repo. Always probe public visibility instead.
                        async with httpx.AsyncClient() as http_client:
                            public = await is_public_repo(http_client, owner_repo=owner_repo)
                        if not public:
                            raise DaimonError(
                                "This repo isn't publicly readable (it's private, or it "
                                "doesn't exist) — paste a GitHub token that can read it "
                                "to bind it."
                            )
                        # No inline PAT -> no per-agent credential is written. The resync
                        # path is agent-overlay-only and never consults a principal-default, so
                        # mark the ref as anonymous rather than implying a fallback exists.
                        ma_secret_ref = "anon:"
                        proof = RepoAccessProof(
                            kind="public", at=now, account_id=self.state.account_id
                        )

                # Persist binding via the dedicated store.
                async with self.runtime.sessionmaker.begin() as session:
                    await set_agent_repo_binding(
                        session,
                        tenant_id=tenant_id,
                        agent_id=agent_uuid,
                        repo_url=url,
                        default_branch=branch,
                        ma_secret_ref=ma_secret_ref,
                        proof=proof,
                    )

                self.state.apply_repo_modal(url=url, branch=branch, pat_last4=pat_last4)
                await call_reconcile_for_panel(self.runtime, self.state, tenant_id=tenant_id)

            from daimon.adapters.discord.agent_setup.edit_view import EditView

            await interaction.edit_original_response(
                view=EditView(
                    self.state,
                    runtime=self.runtime,
                    allowed_user_id=self.allowed_user_id,
                ).bind_render_interaction(interaction, panel=self.state),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if coverage_note is not None:
                await interaction.followup.send(coverage_note, ephemeral=True)
        except DaimonError as err:
            await interaction.followup.send(
                f"Working repo for **{agent_name}**: {err}", ephemeral=True
            )
            return
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
                f"Could not finish connecting the working repo for **{agent_name}**. "
                "Some changes may have been saved. Open `/agent-setup` to check the working repo "
                "before trying again.",
                ephemeral=True,
            )
            return
        _log.info(
            "agent_setup.repo_auth.bound",
            agent_name=agent_name,
            repo_url=url or None,
            branch=branch,
            pat_provided=bool(pat),
        )


class AddSkillModal(discord.ui.Modal, title="Add skill repo"):
    """Add one Skills repo URL. Kicks off sync_agent_skills async."""

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
        if selected is None or not url:
            await interaction.response.defer()
            return
        if await authz.refuse_if_reachable_and_not_admin(
            interaction, runtime=self.runtime, entry=selected
        ):
            return
        await interaction.response.defer()
        tenant_id = await resolve_tenant_for_panel(self.runtime, interaction)
        try:
            self.state.add_skill_repo_pending(url)
            from daimon.adapters.discord.agent_setup.edit_view import EditView

            await interaction.edit_original_response(
                view=EditView(
                    self.state,
                    runtime=self.runtime,
                    allowed_user_id=self.allowed_user_id,
                ).bind_render_interaction(interaction, panel=self.state),
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
                f"Could not refresh the skill repo change for **{agent_name}**. "
                "Open `/agent-setup` to check its skills before trying again.",
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
                    f"Could not finish importing skills for **{agent_name}**. "
                    "Some skills may have been saved. Open `/agent-setup` to check its skills "
                    "before trying again.",
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
