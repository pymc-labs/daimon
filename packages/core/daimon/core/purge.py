"""GDPR purge orchestrator (Phase 17).

Single core entry point that deletes every row referencing a principal (or all
principals under an account) across stores, in FK-safe order, inside a single
transaction. Idempotent on re-run; rolls back fully on any helper raise.

Registry note: the per-store delete sequence is hardcoded inside
`_purge_principal_in_session`. Adding a new principal-scoped table means
appending one helper call here plus one int field on `PurgeReport`. Current
sequence: user_skills -> github_credentials -> agent_github_binding ->
github_oauth_states (both kinds where the table permits) -> routines (platform
only) -> principal_links -> principal row. Account-level deletes (mcp_tokens,
user_configs, accounts) run in `purge_account` after all principal rows are
gone; mcp_tokens is keyed by account_id and is deleted before delete_account so
its NO-ACTION/CASCADE FK to accounts.id is satisfied (Phase 87).

Divergent helper signatures: identity-store `delete_for_principal` is keyed by
UUID; routines `delete_for_principal` is keyed by `(platform, external_id)`
because routines reference platform users by their external id, not the
principal UUID. The orchestrator dispatches manually rather than via a unified
Protocol — see RESEARCH.md A2.

Deliberate carve-outs (D-02):
- `usage_events` and `tenant_user_caps` rows are retained for billing integrity.
  Their `delete_all_for_user` helpers exist and are deliberately uncalled here.
- Uploaded MA skill files (user_skills rows) are our DB ledger: the DB row is
  deleted as part of purge, but the uploaded file content inside Anthropic's
  Managed Agents is retained — guild-shared agents may still reference the
  underlying skill and the MA workspace is not under our delete authority (D-04).
- The user's GitHub-side OAuth grant is not revoked. No GitHub API client enters
  the purge path — we delete only our encrypted credential and oauth-state rows
  from our own DB (D-05).
"""

from __future__ import annotations

import uuid
from typing import Literal

import structlog
from anthropic import APIError, AsyncAnthropic
from daimon.core.ma import SessionDeletionReport, delete_sessions_for_account
from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import agent_github_binding as agent_github_binding_store
from daimon.core.stores import github_credentials as github_credentials_store
from daimon.core.stores import github_oauth_states as github_oauth_states_store
from daimon.core.stores import identity as identity_store
from daimon.core.stores import mcp_tokens as mcp_tokens_store
from daimon.core.stores import routines as routines_store
from daimon.core.stores import user_skills as user_skills_store
from daimon.core.stores.domain import CliPrincipalRow, PlatformPrincipalRow
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

Principal = CliPrincipalRow | PlatformPrincipalRow


class PurgeReport(BaseModel):
    """Per-table rowcount summary returned by purge entry points."""

    model_config = ConfigDict(frozen=True)

    routines: int = 0
    principal_links: int = 0
    cli_principals: int = 0
    platform_principals: int = 0
    user_configs: int = 0
    accounts: int = 0
    user_skills: int = 0
    github_credentials: int = 0
    github_oauth_states: int = 0
    mcp_tokens: int = 0
    agent_github_binding: int = 0

    def merge(self, other: PurgeReport) -> PurgeReport:
        return PurgeReport(
            routines=self.routines + other.routines,
            principal_links=self.principal_links + other.principal_links,
            cli_principals=self.cli_principals + other.cli_principals,
            platform_principals=self.platform_principals + other.platform_principals,
            user_configs=self.user_configs + other.user_configs,
            accounts=self.accounts + other.accounts,
            user_skills=self.user_skills + other.user_skills,
            github_credentials=self.github_credentials + other.github_credentials,
            github_oauth_states=self.github_oauth_states + other.github_oauth_states,
            mcp_tokens=self.mcp_tokens + other.mcp_tokens,
            agent_github_binding=self.agent_github_binding + other.agent_github_binding,
        )


class AccountPurgeResult(BaseModel):
    """Return type of purge_account: DB report paired with upstream session deletion report."""

    model_config = ConfigDict(frozen=True)

    db: PurgeReport
    sessions: SessionDeletionReport = Field(default_factory=SessionDeletionReport)


# FK-safe order: routines -> principal_links -> principal row. Append new
# principal-scoped helpers above the principal-row delete; account-level
# deletes happen in `purge_account` after this returns.
async def _purge_principal_in_session(
    session: AsyncSession, *, principal: Principal
) -> PurgeReport:
    """Delete every row referencing `principal` on the given open session.

    Does NOT open a transaction — caller owns the begin() block. Failures
    propagate so the caller's transaction rolls back.
    """
    if isinstance(principal, PlatformPrincipalRow):
        routines_count = await routines_store.delete_for_principal(
            session,
            tenant_id=principal.tenant_id,
            external_id=principal.external_id,
        )
        kind: Literal["cli", "platform"] = "platform"
        # Platform principals have oauth-state rows keyed (platform, external_id).
        # tenant_id is required: external_id is NOT globally unique across
        # platforms — Slack user ids are workspace-scoped, so `U123` in two
        # workspaces are two different humans. A tenant-agnostic delete would
        # erase another tenant's in-flight (10-min-TTL) handshake rows. The
        # narrow D-06 "ghost rows under a stale tenant_id" completeness gap
        # (re-key drift) is accepted — those rows expire unused.
        oauth_states_count = await github_oauth_states_store.delete_states_for_platform_user(
            session,
            platform=principal.platform,
            platform_user_id=principal.external_id,
            tenant_id=principal.tenant_id,
        )
    else:
        routines_count = 0
        kind = "cli"
        # The CLI auth flow writes oauth-state rows with platform="cli",
        # platform_user_id=<os_user> (adapters/cli/commands/auth.py). D-07 covers
        # both principal kinds where the table permits — CLI principals are included.
        # tenant_id scoping is a deliberate D-06 carve-out: os_user is NOT
        # globally unique (two unrelated people can both be `ubuntu`), so a
        # tenant-agnostic delete would erase another account's handshake rows.
        # Trade-off accepted: cli handshake ghost rows stranded under a stale
        # tenant_id are left for a separate sweep (they expire from use after
        # the 10-minute TTL and never join back to a principal).
        oauth_states_count = await github_oauth_states_store.delete_states_for_platform_user(
            session,
            platform="cli",
            platform_user_id=principal.os_user,
            tenant_id=principal.tenant_id,
        )

    # user_skills and github_credentials are keyed by principal_id alone — both
    # principal kinds own rows in these tables (D-07).
    user_skills_count = await user_skills_store.delete_user_skills_for_principal(
        session, principal_id=principal.id
    )
    github_credentials_count = await github_credentials_store.delete_credential_for_principal(
        session, principal_id=principal.id
    )
    agent_github_binding_count = await agent_github_binding_store.delete_for_principal(
        session, principal_id=principal.id
    )

    links_count = await identity_store.delete_principal_links_for_principal(
        session, principal_id=principal.id, kind=kind
    )
    principal_count = await identity_store.delete_for_principal(
        session, principal_id=principal.id, kind=kind
    )

    return PurgeReport(
        routines=routines_count,
        principal_links=links_count,
        cli_principals=principal_count if kind == "cli" else 0,
        platform_principals=principal_count if kind == "platform" else 0,
        user_skills=user_skills_count,
        github_credentials=github_credentials_count,
        github_oauth_states=oauth_states_count,
        agent_github_binding=agent_github_binding_count,
    )


async def purge_principal(
    *,
    sm: async_sessionmaker[AsyncSession],
    principal_id: uuid.UUID,
    kind: Literal["cli", "platform"],
) -> PurgeReport:
    """Delete every row for `(principal_id, kind)`. Idempotent on re-run."""
    async with sm() as session, session.begin():
        principal = await identity_store.get_principal_by_id(
            session, principal_id=principal_id, kind=kind
        )
        if principal is None:
            return PurgeReport()
        return await _purge_principal_in_session(session, principal=principal)


async def purge_account(
    *,
    sm: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    anthropic: AsyncAnthropic | None = None,
) -> AccountPurgeResult:
    """Delete every principal under `account_id`, then user_config + account.

    Account-level deletes (user_config, accounts) run AFTER all principal-row
    deletes so the FK RESTRICT on `accounts.id <- principals.account_id` is
    satisfied. Tenant-scoped tables (tenants, channel_config, tenant_config)
    are NOT touched.

    When `anthropic` is provided, attempts upstream hard-deletion of all MA
    sessions tagged for `account_id` AFTER the DB transaction commits (D-07:
    DB purge is never rolled back by an upstream failure). Sessions are
    enumerated per tenant, across EVERY tenant any linked principal belongs
    to — `principal_links` permits an account to span tenants.
    """
    async with sm() as session, session.begin():
        cli_list = await identity_store.list_cli_principals_for_account(
            session, account_id=account_id
        )
        pp_list = await identity_store.list_platform_principals_for_account(
            session, account_id=account_id
        )
        # Capture every distinct tenant before the session closes — each
        # tenant's agents must be enumerated for upstream session deletion.
        tenant_ids: set[uuid.UUID] = {principal.tenant_id for principal in (*cli_list, *pp_list)}

        report = PurgeReport()
        for principal in (*cli_list, *pp_list):
            sub = await _purge_principal_in_session(session, principal=principal)
            report = report.merge(sub)

        # Account-scoped (keyed by account_id, not principal): delete the
        # per-agent MCP token rows BEFORE delete_account — they reference
        # accounts.id, so leaving them trips the FK and rolls back the purge.
        mcp_tokens_count = await mcp_tokens_store.delete_tokens_for_account(
            session, account_id=account_id
        )
        user_cfg_count = await accounts_store.delete_user_config_for_account(
            session, account_id=account_id
        )
        account_count = await accounts_store.delete_account(session, account_id=account_id)
        db_report = report.merge(
            PurgeReport(
                mcp_tokens=mcp_tokens_count,
                user_configs=user_cfg_count,
                accounts=account_count,
            )
        )

    # DB transaction committed. Upstream deletion is best-effort (D-07),
    # looped over every tenant the account's principals belonged to.
    # Deliberate boundary catch: the DB purge has already committed, so an
    # upstream APIError must NOT propagate — the caller would misreport a
    # completed, irreversible erasure as failed. Fold the failure into the
    # sessions report instead (upstream_error=True) and log for the operator;
    # a failure in one tenant does not skip the remaining tenants.
    if anthropic is not None and tenant_ids:
        deleted = 0
        failed = 0
        upstream_error = False
        for tenant_id in sorted(tenant_ids):
            try:
                sub = await delete_sessions_for_account(
                    anthropic, tenant_id=tenant_id, account_id=account_id
                )
            except APIError as err:
                log.warning(
                    "purge.upstream_sessions_failed",
                    account_id=str(account_id),
                    tenant_id=str(tenant_id),
                    error=str(err),
                )
                upstream_error = True
                continue
            deleted += sub.deleted
            failed += sub.failed
        return AccountPurgeResult(
            db=db_report,
            sessions=SessionDeletionReport(
                deleted=deleted, failed=failed, upstream_error=upstream_error
            ),
        )

    return AccountPurgeResult(db=db_report)
