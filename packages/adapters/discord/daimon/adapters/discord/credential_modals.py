"""EnvCredentialModal / McpCredentialModal / RepoBindModal — the three
credential-button modals.

Three separate modals, not one type-dispatching modal: an env secret, an MCP
auth token, and a repo binding are different resources with different write
paths (`put_agent_file` vs `add_external_mcp_credential` vs
`agent_repo_binding.set_binding`), mirroring the modals that already exist
for the same writes on the setup panel (`agent_setup/credentials.py`'s
`PasteSecretModal`, `agent_setup/modals_mcp.py`'s `AddMcpModal`,
`agent_setup/modals.py`'s `RepoAuthModal`). `EnvCredentialModal` and
`McpCredentialModal` each collect exactly ONE field — the secret value
itself — because every routing field (agent, key/server name) is already
fixed by the consumed `credential_requests` row; the user never retypes it.
`RepoBindModal` collects two fields (branch, optional token) because a repo
binding has two writable parts and only one of them is a secret; the repo
itself is likewise fixed by the row, never retyped.

Secret hygiene, matching the structural guarantees `PasteSecretModal` already
documents:
- the value never reaches a log record (env logs the key name only; MCP and
  repo log a masked tail only),
- the value never reaches a `custom_id` (the button's custom_id carries only
  the opaque request token, minted before any modal exists),
- the value never reaches a container/embed (a Modal TextInput has no
  render surface other than the ephemeral confirmation, which never echoes
  the value back),
- there is no URL-fetch path and no attachment path.

The atomic single-use consume runs BEFORE every write, so a request can
only ever produce one write no matter how many times its modal is
(re)submitted — the loser of a race, or any resubmission, gets `None` back
and writes nothing.

`RepoBindModal`'s write is additionally admin-gated on a shared agent: token
consumption alone is sufficient authorization for an env secret or an MCP
token (the requester supplies a value only they hold, scoped to one key on
one agent), but a repo binding changes what code the agent clones and runs
and, on a shared agent, reaches every member of the install. So its
`on_submit` also runs
`credential_repo_bind.refuse_if_shared_and_not_admin_for_request` — once
here, right after `defer` and before the consume, and once more as a
pre-filter in `credential_button.py`'s `callback`, before the modal is even
opened.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import anthropic
import httpx
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_skill_params import BetaManagedAgentsSkillParams
from daimon.adapters.discord.agent_setup.credentials import (
    _MAX_SECRET_VALUE_BYTES,  # pyright: ignore[reportPrivateUsage]  # reusing PasteSecretModal's byte cap rather than inventing a second number
)
from daimon.adapters.discord.agent_setup.write import mask_tail
from daimon.adapters.discord.credential_repo_bind import (
    refuse_if_shared_and_not_admin_for_request,
    resolve_repo_binding_credential,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.credential_requests import split_skill_repo_target
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid, find_attach_mount_collision
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.defaults.spec_merge import merge_skills_with_ma
from daimon.core.errors import DaimonError
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.github_visibility import pat_can_access_repo
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.mcp_attach import attach_mcp_server_to_agent
from daimon.core.mcp_vault import add_external_mcp_credential
from daimon.core.skills.pipeline import run_skill_sync
from daimon.core.stores import credential_requests
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.stores.agent_repo_binding import set_binding
from daimon.core.stores.domain import CredentialRequestRow

import discord

_log = structlog.get_logger()

_NO_LONGER_VALID = "This request is no longer valid — ask again."


class EnvCredentialModal(discord.ui.Modal, title="Add secret"):
    """Add secrets modal: one value, atomic consume, existing agent_files write."""

    def __init__(self, *, runtime: DiscordRuntime, request_row: CredentialRequestRow) -> None:
        super().__init__()
        self._runtime = runtime
        self._row = request_row
        self.value_input: discord.ui.TextInput[EnvCredentialModal] = discord.ui.TextInput(
            label="Secret value",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
            placeholder="usable by everyone who talks to this agent",
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw_value = str(self.value_input.value or "")

        if not raw_value.strip():
            await interaction.followup.send(
                "Secret value cannot be empty — try again.", ephemeral=True
            )
            return
        if len(raw_value.encode()) > _MAX_SECRET_VALUE_BYTES:
            await interaction.followup.send(
                f"Secret value is too large. Max {_MAX_SECRET_VALUE_BYTES} bytes.",
                ephemeral=True,
            )
            return

        now = datetime.now(UTC)
        try:
            async with self._runtime.sessionmaker() as session, session.begin():
                consumed_row = await credential_requests.consume_credential_request(
                    session, token=self._row.token, now=now
                )
                if consumed_row is None:
                    await interaction.followup.send(_NO_LONGER_VALID, ephemeral=True)
                    return
                await put_agent_file(
                    session,
                    tenant_id=consumed_row.tenant_id,
                    agent_id=consumed_row.agent_id,
                    key=consumed_row.target,
                    content=raw_value,
                )
        except Exception:
            _log.exception("credential_modal.env_write_failed", key=self._row.target)
            await interaction.followup.send(
                "Something went wrong — please try again.", ephemeral=True
            )
            return

        # Log the key NAME only — never the value.
        _log.info("credential_modal.env.submit", key=consumed_row.target)
        await interaction.followup.send(
            f"Added `{consumed_row.target}`. Takes effect on the next session — "
            "anyone who talks to this agent can use it.",
            ephemeral=True,
        )


class McpCredentialModal(discord.ui.Modal, title="Add MCP credential"):
    """Add MCP credential modal: one token, atomic consume, existing vault write."""

    def __init__(self, *, runtime: DiscordRuntime, request_row: CredentialRequestRow) -> None:
        super().__init__()
        self._runtime = runtime
        self._row = request_row
        self.token_input: discord.ui.TextInput[McpCredentialModal] = discord.ui.TextInput(
            label="Auth token",
            required=True,
            max_length=255,
            placeholder="usable by everyone who talks to this agent",
        )
        self.add_item(self.token_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        token_value = str(self.token_input.value or "")

        if not token_value.strip():
            await interaction.followup.send(
                "Auth token cannot be empty — try again.", ephemeral=True
            )
            return

        public_url_setting = self._runtime.settings.mcp.public_url
        jwt_secret_setting = self._runtime.settings.mcp.jwt_secret
        if public_url_setting is None or jwt_secret_setting is None:
            await interaction.followup.send(
                "daimon-mcp is not configured (public_url / jwt_secret missing) — "
                "credential not written.",
                ephemeral=True,
            )
            return

        now = datetime.now(UTC)
        async with self._runtime.sessionmaker() as session, session.begin():
            consumed_row = await credential_requests.consume_credential_request(
                session, token=self._row.token, now=now
            )
        if consumed_row is None:
            await interaction.followup.send(_NO_LONGER_VALID, ephemeral=True)
            return

        mcp_server_url = consumed_row.mcp_server_url
        if mcp_server_url is None:
            _log.error("credential_modal.mcp_missing_server_url", token_tail=self._row.token[-4:])
            await interaction.followup.send(
                "This request is missing its server URL — please ask again.", ephemeral=True
            )
            return

        _log.info(
            "credential_modal.mcp.submit",
            mcp_server_url=mcp_server_url,
            token_masked=mask_tail(token_value),
        )
        try:
            await add_external_mcp_credential(
                self._runtime.anthropic,
                account_id=consumed_row.account_id,
                agent_id=consumed_row.agent_id,
                jwt_secret=jwt_secret_setting.get_secret_value().encode(),
                public_url=str(public_url_setting),
                mcp_server_url=mcp_server_url,
                token=token_value,
                now=now,
                session_factory=self._runtime.sessionmaker,
            )
        except Exception as err:
            _log.exception(
                "credential_modal.mcp_write_failed",
                mcp_server_url=mcp_server_url,
                err_type=type(err).__name__,
            )
            # Surface only the exception class name — never the stringified
            # exception, which for SDK/network errors can carry the request
            # envelope (a token-leak surface). Full traceback goes to structlog.
            await interaction.followup.send(
                "Credential request consumed, but storing the auth token failed "
                f"(`{type(err).__name__}`). Ask for a new request to retry.",
                ephemeral=True,
            )
            return

        # The vault credential alone is inert: MA rejects an agent whose
        # mcp_servers are not each referenced by an mcp_toolset, so a token
        # stored against a server the agent never declares is unreachable.
        # This tool is documented as the replacement for attach_mcp_server on
        # auth-required servers, so it owes the attach too (#49) — the vault
        # write happens first, because a declared server with no credential
        # advertises calls that fail.
        agent = await find_agent_by_derived_uuid(
            self._runtime.anthropic,
            tenant_id=consumed_row.tenant_id,
            agent_id=consumed_row.agent_id,
        )
        if agent is None:
            _log.error(
                "credential_modal.mcp_agent_not_found",
                agent_id=str(consumed_row.agent_id),
            )
            await interaction.followup.send(
                "Auth token stored, but the agent could not be found to attach "
                f"`{mcp_server_url}` to it. The server is not connected yet.",
                ephemeral=True,
            )
            return
        try:
            await attach_mcp_server_to_agent(
                self._runtime.anthropic,
                agent.id,
                server_name=consumed_row.target,
                url=mcp_server_url,
            )
        except Exception as err:
            # Partial state is real and must not be reported as success: the
            # token is stored, the server is not attached. Exception class name
            # only, for the same token-leak reason as the vault-write branch.
            _log.exception(
                "credential_modal.mcp_attach_failed",
                mcp_server_url=mcp_server_url,
                err_type=type(err).__name__,
            )
            await interaction.followup.send(
                f"Auth token stored, but attaching `{mcp_server_url}` to the agent "
                f"failed (`{type(err).__name__}`). The server is not connected yet — "
                "ask the agent to attach it, or request a new credential to retry.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"MCP credential added for `{mcp_server_url}` and attached as "
            f"`{consumed_row.target}`. Anyone who talks to this agent can use it.",
            ephemeral=True,
        )


class SkillRepoModal(discord.ui.Modal, title="Import skills"):
    """Collect a GitHub token for a PRIVATE skill repo, then run the import.

    Deliberately NOT a `RepoBindModal` variant, even though both collect a
    GitHub token into the same per-agent overlay. Two differences are the
    whole point of the separate class:

    1. No `set_binding`. Importing skills from a repo must not also tell the
       agent to check that repo out. Sharing the store is intended; sharing
       the binding write is the bug this exists to avoid.
    2. The token is verified against the SKILL repo (`target`), not the
       agent's clone repo — for a skill import those are routinely different
       repos, and validating the wrong one both refuses good tokens and
       accepts ones that cannot read what is about to be fetched.

    Because the credential lands in the same overlay `get_pat(agent_id=…)`
    resolves, a user who already pasted a token that can read this repo is
    never asked twice — the agent should attempt `sync_skills` first and only
    reach for this button on an actual no-credential failure.

    No admin gate. This mirrors the env/mcp kinds rather than the repo kind:
    the repo kind is admin-gated on a shared agent because a binding changes
    what code the agent runs for every member, and that reasoning does not
    reach an import into the shared skill library, which `sync_skills` itself
    already gates with `_require_admin` at request time.
    """

    def __init__(self, *, runtime: DiscordRuntime, request_row: CredentialRequestRow) -> None:
        super().__init__()
        self._runtime = runtime
        self._row = request_row
        url, _branch, _path = split_skill_repo_target(request_row.target)
        self.pat_in: discord.ui.TextInput[SkillRepoModal] = discord.ui.TextInput(
            label="GitHub token",
            required=True,
            # Discord rejects the whole modal with 50035 above 4000, so this is
            # a UI character cap and NOT _MAX_SECRET_VALUE_BYTES (4096), which
            # is a byte cap enforced on submit. EnvCredentialModal keeps the two
            # separate for the same reason.
            max_length=4000,
            placeholder=f"Needs read access to {normalize_owner_repo(url)}",
        )
        self.add_item(self.pat_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        pat = str(self.pat_in.value or "").strip()
        if not pat:
            await interaction.followup.send("Enter a GitHub token.", ephemeral=True)
            return

        now = datetime.now(UTC)
        async with self._runtime.sessionmaker() as session, session.begin():
            consumed_row = await credential_requests.consume_credential_request(
                session, token=self._row.token, now=now
            )
        if consumed_row is None:
            await interaction.followup.send(_NO_LONGER_VALID, ephemeral=True)
            return

        url, branch, path = split_skill_repo_target(consumed_row.target)
        # The token appears only as a masked tail, never in full, and never
        # the (now-consumed) request token either.
        _log.info(
            "credential_modal.skill_repo.submit",
            repo_url=url,
            branch=branch,
            path=path,
            pat_masked=mask_tail(pat),
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                # Verify BEFORE storing: a token that cannot read this repo is
                # not a credential for it, and storing it would shadow a
                # working one on the next `get_pat` (the overlay is
                # last-write-wins, and tier 1 short-circuits the rest).
                if not await pat_can_access_repo(
                    http_client, owner_repo=normalize_owner_repo(url), pat=pat
                ):
                    await interaction.followup.send(
                        f"That token cannot read `{normalize_owner_repo(url)}`. Nothing was "
                        "stored, and the request was used up — ask again to retry.",
                        ephemeral=True,
                    )
                    return
                # Reuse the repo kind's resolver rather than calling
                # `store_inline_pat` directly: it returns the `ma_secret_ref`
                # AND the access proof that `set_binding` requires, so the two
                # writes cannot disagree about what was established.
                ma_secret_ref, proof = await resolve_repo_binding_credential(
                    self._runtime,
                    http_client,
                    agent_id=consumed_row.agent_id,
                    account_id=consumed_row.account_id,
                    repo_url=url,
                    pasted_pat=pat,
                    now=now,
                )
                # Without this row the stored PAT is unreachable: the skill-sync
                # resolver finds a per-agent token by walking this tenant's
                # `agent_repo_binding` rows FOR THIS REPO, not by agent alone
                # (the session JWT carries no agent_id claim). Storing the
                # credential without binding the repo is what made a pasted
                # token look ignored — every later sync resolved `token=None`,
                # got GitHub's 404, and asked for the credential again.
                async with self._runtime.sessionmaker.begin() as session:
                    await set_binding(
                        session,
                        tenant_id=consumed_row.tenant_id,
                        agent_id=consumed_row.agent_id,
                        repo_url=url,
                        default_branch=branch,
                        ma_secret_ref=ma_secret_ref,
                        proof=proof,
                    )
                outcomes = await run_skill_sync(
                    self._runtime.anthropic,
                    http_client,
                    url=url,
                    branch=branch,
                    path=path,
                    tenant_id=consumed_row.tenant_id,
                    token=pat,
                )
        except DaimonError as err:
            # Written for the user by the raiser — surface verbatim. The
            # credential is already stored and still good; only the import
            # leg failed, so say which half survived.
            await interaction.followup.send(
                f"Token stored, but the import failed: {err} The request was used up — "
                "ask again to retry the import.",
                ephemeral=True,
            )
            return
        except Exception as err:
            _log.exception(
                "credential_modal.skill_repo_sync_failed",
                repo_url=url,
                err_type=type(err).__name__,
            )
            # Class name only — a stringified SDK/network error can carry the
            # request envelope, which is a token-leak surface.
            await interaction.followup.send(
                f"Token stored, but the import failed (`{type(err).__name__}`). "
                "Ask again to retry the import.",
                ephemeral=True,
            )
            return

        attach_note = await self._attach_to_requested_agent(
            tenant_id=consumed_row.tenant_id,
            agent_id=consumed_row.agent_id,
            outcomes=outcomes,
        )
        await interaction.followup.send(
            f"Imported {len(outcomes)} skill(s) from `{normalize_owner_repo(url)}`. "
            f"{attach_note} The token is stored and the repo is bound, so future "
            "imports from it will not ask again.",
            ephemeral=True,
        )

    async def _attach_to_requested_agent(
        self,
        *,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        outcomes: list[ResourceOutcome],
    ) -> str:
        """Attach the just-imported skills to the agent this request named.

        Importing puts skills in the tenant's shared library; it does not put
        them on an agent. The request row already names the agent, so doing
        only the import leaves the user staring at an agent with no skills and
        no way to tell that anything worked.

        Returns prose rather than raising: the import has already succeeded by
        the time this runs, so a failure here is partial and both halves must
        be reported truthfully.
        """
        skill_ids = sorted(
            outcome.anthropic_id
            for outcome in outcomes
            if outcome.anthropic_id is not None
            and outcome.action in (Action.CREATED, Action.UPDATED)
        )
        if not skill_ids:
            return "Nothing new to attach."
        agent = await find_agent_by_derived_uuid(
            self._runtime.anthropic, tenant_id=tenant_id, agent_id=agent_id
        )
        if agent is None:
            return "Could not attach: that agent no longer exists. The skills are in the library."
        new_skills: list[BetaManagedAgentsSkillParams] = [
            {"type": "custom", "skill_id": skill_id} for skill_id in skill_ids
        ]

        async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
            merged = merge_skills_with_ma(new_skills, fresh)
            collision = await find_attach_mount_collision(
                self._runtime.anthropic, tenant_id=tenant_id, skills=merged
            )
            if collision is not None:
                raise DaimonError(f"cannot attach: {collision}")
            return await self._runtime.anthropic.beta.agents.update(
                fresh.id, version=fresh.version, skills=merged
            )

        try:
            await update_agent_with_version_retry(self._runtime.anthropic, agent.id, _apply)
        except (DaimonError, anthropic.APIStatusError) as err:
            _log.warning(
                "credential_modal.skill_repo_attach_failed",
                agent_id=str(agent_id),
                err_type=type(err).__name__,
            )
            return f"Imported, but attaching to `{agent.name}` failed ({type(err).__name__})."
        return f"Attached {len(skill_ids)} to `{agent.name}`."


class RepoBindModal(discord.ui.Modal, title="Bind repo"):
    """Bind repo modal: branch + optional token, gate, atomic consume, then
    the shared credential resolution and the binding write.

    The repo itself is never retyped here — it is fixed by the consumed
    row's `target`, exactly as the env key and MCP server name are for the
    two sibling modals. Unlike them, submitting this one is additionally
    gated on `credential_repo_bind.refuse_if_shared_and_not_admin_for_request`
    (see the module docstring): a member who was an admin, or whose target
    was private, when the button was clicked may have lost either between
    click and submit, so the gate runs again here, immediately after
    `defer` and before the consume, rather than being trusted from the
    pre-filter alone.
    """

    def __init__(self, *, runtime: DiscordRuntime, request_row: CredentialRequestRow) -> None:
        super().__init__()
        self._runtime = runtime
        self._row = request_row
        self.branch_in: discord.ui.TextInput[RepoBindModal] = discord.ui.TextInput(
            label="Branch",
            default="main",
            max_length=255,
        )
        self.pat_in: discord.ui.TextInput[RepoBindModal] = discord.ui.TextInput(
            label="GitHub token (optional)",
            required=False,
            max_length=255,
            placeholder="Leave blank for a public repo",
        )
        self.add_item(self.branch_in)
        self.add_item(self.pat_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if await refuse_if_shared_and_not_admin_for_request(
            interaction,
            runtime=self._runtime,
            tenant_id=self._row.tenant_id,
            agent_id=self._row.agent_id,
        ):
            return

        branch = str(self.branch_in.value or "").strip() or "main"
        pat = str(self.pat_in.value or "").strip()

        now = datetime.now(UTC)
        async with self._runtime.sessionmaker() as session, session.begin():
            consumed_row = await credential_requests.consume_credential_request(
                session, token=self._row.token, now=now
            )
        if consumed_row is None:
            await interaction.followup.send(_NO_LONGER_VALID, ephemeral=True)
            return

        # Log the repo and branch, and the token ONLY as a masked tail when
        # present — never the plain value, never the (now-consumed) request
        # token.
        _log.info(
            "credential_modal.repo.submit",
            repo_url=consumed_row.target,
            branch=branch,
            pat_masked=mask_tail(pat) if pat else None,
        )

        try:
            async with httpx.AsyncClient() as http_client:
                ma_secret_ref, proof = await resolve_repo_binding_credential(
                    self._runtime,
                    http_client,
                    agent_id=consumed_row.agent_id,
                    account_id=consumed_row.account_id,
                    repo_url=consumed_row.target,
                    pasted_pat=pat or None,
                    now=now,
                )
            async with self._runtime.sessionmaker.begin() as session:
                await set_binding(
                    session,
                    tenant_id=consumed_row.tenant_id,
                    agent_id=consumed_row.agent_id,
                    repo_url=consumed_row.target,
                    default_branch=branch,
                    ma_secret_ref=ma_secret_ref,
                    proof=proof,
                )
        except DaimonError as err:
            # This copy is written for the user -- surface it verbatim, plus
            # the fact that the request itself was used up either way.
            await interaction.followup.send(
                f"{err} The request was used up — ask again to retry.",
                ephemeral=True,
            )
            return
        except Exception as err:
            _log.exception(
                "credential_modal.repo_write_failed",
                repo_url=consumed_row.target,
                err_type=type(err).__name__,
            )
            # Surface only the exception class name — never the stringified
            # exception, which for SDK/network errors can carry the request
            # envelope (a token-leak surface). Full traceback goes to structlog.
            await interaction.followup.send(
                "Credential request consumed, but binding the repo failed "
                f"(`{type(err).__name__}`). Ask for a new request to retry.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Bound `{consumed_row.target}` on `{branch}`. Takes effect on the next session.",
            ephemeral=True,
        )
