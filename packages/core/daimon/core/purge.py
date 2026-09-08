"""GDPR purge orchestrator.

Single core entry point that deletes every row referencing a principal (or all
principals under an account) across stores, in FK-safe order, inside a single
transaction. Idempotent on re-run; rolls back fully on any helper raise.

Registry note: the per-store delete sequence is hardcoded inside
`_purge_principal_in_session`. Adding a new principal-scoped table means
appending one helper call here plus one int field on `PurgeReport`. Current
sequence: user_skills -> github_credentials -> agent_github_binding ->
github_oauth_states (both kinds where the table permits) -> credential_requests
(both kinds, platform-user-scoped like github_oauth_states) -> wizard_session
(both kinds, platform-user-scoped like credential_requests) -> message_feedback
(platform only, platform-user-scoped) -> routines (platform only) ->
principal_links -> principal row. Account-level deletes (mcp_tokens,
message_feedback, user_configs, accounts) run in `purge_account` after all
principal rows are gone; mcp_tokens and message_feedback are keyed by
account_id and are deleted before delete_account so their CASCADE FKs to
accounts.id are satisfied. message_feedback is deleted by an account-id
predicate OR'd with the account's (tenant, platform-user) keys, because a
vote cast before the person had an account row carries a null account id.

message_feedback is the one table deleted on BOTH paths, because it is the
one table whose rows can carry either identity key. The principal path
deletes only the principal's own (tenant, platform-user) rows; the account
path then sweeps whatever the account-id predicate still reaches. The two
passes cannot double-count — the second only sees what the first left — so
the summed report still equals `collect_purge_preview`'s single count.

wizard_session rides the platform-user-scoped delete path rather than an
accounts.id cascade: the row's identity key is the requester's platform user
id (a Discord/Slack tap must resolve back to a form regardless of whether an
account row exists yet), and the nullable accounts FK on the table exists only
to make the schema-reflecting drift guard see the table, not to delete through.

credential_requests carries a Discord snowflake in requester_platform_user_id
with no cleanup sweep by design (the table is one row per credential request,
TTL-bounded relevance) — erasure therefore rides this purge path exactly like
the OAuth handshake table, rather than a scheduled job. Precedent sweepers
(`mcp_credential_sweep.py`, `pending_file_sweeper.py`) exist if volume ever
justifies one instead.

Divergent helper signatures: identity-store `delete_for_principal` is keyed by
UUID; routines `delete_for_principal` is keyed by `(platform, external_id)`
because routines reference platform users by their external id, not the
principal UUID. The orchestrator dispatches manually rather than via a unified
Protocol — see RESEARCH.md A2.

Deliberate carve-outs:
- Hosted chart artifacts in operator-owned object storage are not deleted by
  account purge; operators must enforce retention with a bucket lifecycle rule.
- `usage_events` and `tenant_user_caps` rows are retained for billing integrity.
  Their `delete_all_for_user` helpers exist and are deliberately uncalled here.
- Uploaded MA skill files (user_skills rows) are our DB ledger: the DB row is
  deleted as part of purge, but the uploaded file content inside Anthropic's
  Managed Agents is retained — guild-shared agents may still reference the
  underlying skill and the MA workspace is not under our delete authority.
- The user's GitHub-side OAuth grant is not revoked. No GitHub API client enters
  the purge path — we delete only our encrypted credential and oauth-state rows
  from our own DB.
- Agent memory stores are agent-scoped and shared across all workspace users,
  so account purge does NOT touch them — archiving one because a single member
  invoked erasure would destroy the guild's shared agent memory. They may
  retain information about the purged user.
- message_feedback rows cast in a tenant where the person has NO principal are
  out of reach of an erasure run from another tenant. This is the one table
  that records rows for someone with no principal anywhere: the reaction path
  deliberately never mints one for a bystander, so a vote (and any free text
  they then submit) lands with account_id = NULL, attributable only by
  (tenant_id, platform_user_id). An erasure invoked in guild A derives its
  platform-user keys from the account's own principals, which do not include
  (tenant_B, user), so guild B's row survives. Unlike the tenant-scoped
  oauth-state ghost rows above, these are durable and hold free text.
  Remediation is guild-local and self-service: running the privacy purge
  inside guild B mints a tenant-B principal, which brings (tenant_B, user)
  into the key set and erases those rows. Deliberately not closed by a
  platform-scoped, tenant-agnostic predicate: it would be correct only for
  platforms whose external ids are globally unique (Discord snowflakes, not
  Slack workspace-scoped user ids), so it would make erasure's blast radius
  depend on the platform — a worse contract than one documented, reachable
  remediation path.
"""

from __future__ import annotations

import uuid
from typing import Literal

import structlog
from anthropic import APIError, AsyncAnthropic
from daimon.core.ma import SessionDeletionReport, delete_sessions_for_account
from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import agent_github_binding as agent_github_binding_store
from daimon.core.stores import credential_requests as credential_requests_store
from daimon.core.stores import github_credentials as github_credentials_store
from daimon.core.stores import github_oauth_states as github_oauth_states_store
from daimon.core.stores import identity as identity_store
from daimon.core.stores import mcp_tokens as mcp_tokens_store
from daimon.core.stores import message_feedback as message_feedback_store
from daimon.core.stores import routines as routines_store
from daimon.core.stores import slack_turn_contexts as slack_turn_contexts_store
from daimon.core.stores import slack_user_tokens as slack_user_tokens_store
from daimon.core.stores import support_escalation as support_escalation_store
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import user_skills as user_skills_store
from daimon.core.stores import wizard_session as wizard_session_store
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
    slack_user_tokens: int = 0
    slack_turn_contexts: int = 0
    credential_requests: int = 0
    wizard_sessions: int = 0
    message_feedback: int = 0
    support_escalations: int = 0

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
            slack_user_tokens=self.slack_user_tokens + other.slack_user_tokens,
            slack_turn_contexts=self.slack_turn_contexts + other.slack_turn_contexts,
            credential_requests=self.credential_requests + other.credential_requests,
            wizard_sessions=self.wizard_sessions + other.wizard_sessions,
            message_feedback=self.message_feedback + other.message_feedback,
            support_escalations=self.support_escalations + other.support_escalations,
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
        # narrow "ghost rows under a stale tenant_id" completeness gap
        # (re-key drift) is accepted — those rows expire unused.
        oauth_states_count = await github_oauth_states_store.delete_states_for_platform_user(
            session,
            platform=principal.platform,
            platform_user_id=principal.external_id,
            tenant_id=principal.tenant_id,
        )
        # credential_requests carries the same non-globally-unique
        # platform_user_id caveat as oauth_states above — always tenant-scoped.
        credential_requests_count = (
            await credential_requests_store.delete_credential_requests_for_platform_user(
                session,
                platform_user_id=principal.external_id,
                tenant_id=principal.tenant_id,
            )
        )
        # wizard_session carries the same non-globally-unique platform_user_id
        # caveat as credential_requests above — always tenant-scoped.
        wizard_sessions_count = await wizard_session_store.delete_wizard_sessions_for_platform_user(
            session,
            platform_user_id=principal.external_id,
            tenant_id=principal.tenant_id,
        )
        # message_feedback carries the SAME (tenant, platform-user) identity key
        # as the three tables above, so it must be purged on this path too:
        # a vote row can carry account_id = NULL (the reaction path never mints
        # a principal), leaving votes AND their attached free text behind if
        # only the account-level pass in purge_account deleted them.
        # Deliberately the narrow platform-user-keyed helper, not the
        # account-keyed one: purging ONE principal must not erase the same
        # account's votes under another tenant.
        message_feedback_count = (
            await message_feedback_store.delete_message_feedback_for_platform_user(
                session,
                tenant_id=principal.tenant_id,
                platform_user_id=principal.external_id,
            )
        )
        # support_escalations carries the SAME (tenant, platform-user) key and
        # the same account_id = NULL hazard: somebody can ask for help without
        # ever having taken a turn, so an account-keyed pass alone would leave
        # the request AND its free-text note behind. The note is unsolicited
        # personal text, so this is the same erasure obligation the vote text
        # carries, not a bookkeeping nicety.
        support_escalations_count = (
            await support_escalation_store.delete_support_escalations_for_platform_user(
                session,
                tenant_id=principal.tenant_id,
                platform_user_id=principal.external_id,
            )
        )
    else:
        routines_count = 0
        kind = "cli"
        # The CLI auth flow writes oauth-state rows with platform="cli",
        # platform_user_id=<os_user> (adapters/cli/commands/auth.py). Both
        # principal kinds own rows in the tables where the schema permits it —
        # CLI principals are included.
        # tenant_id scoping is a deliberate carve-out: os_user is NOT
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
        # CLI principals never own credential_requests rows in practice (the
        # credential button flow is Discord-only), so this always reports 0 —
        # kept for symmetry with the platform branch and future-proofing.
        credential_requests_count = (
            await credential_requests_store.delete_credential_requests_for_platform_user(
                session,
                platform_user_id=principal.os_user,
                tenant_id=principal.tenant_id,
            )
        )
        # CLI principals never own wizard_session rows in practice (the wizard
        # flow is Discord/Slack-only), so this always reports 0 — kept for
        # symmetry with the platform branch and future-proofing.
        wizard_sessions_count = await wizard_session_store.delete_wizard_sessions_for_platform_user(
            session,
            platform_user_id=principal.os_user,
            tenant_id=principal.tenant_id,
        )
        # A vote is always cast by a platform user reacting in a guild, so a
        # CLI principal owns no message_feedback rows — nothing to delete, and
        # no symmetric call here: os_user is not a reaction identity, and
        # running one would risk deleting an unrelated platform user's rows
        # that happen to share the string.
        message_feedback_count = 0
        # Escalation is a reaction-then-modal path in a guild, so a CLI
        # principal owns no support_escalations rows — same reasoning, and the
        # same refusal to match on os_user, as message_feedback above.
        support_escalations_count = 0

    # user_skills and github_credentials are keyed by principal_id alone — both
    # principal kinds own rows in these tables.
    user_skills_count = await user_skills_store.delete_user_skills_for_principal(
        session, principal_id=principal.id
    )
    github_credentials_count = await github_credentials_store.delete_credential_for_principal(
        session, principal_id=principal.id
    )
    agent_github_binding_count = await agent_github_binding_store.delete_for_principal(
        session, principal_id=principal.id
    )

    # slack_user_tokens is keyed by (team_id, slack_user_id), not principal_id.
    # team_id = Tenant.external_id (the folded workspace_id) resolved at
    # runtime via principal.tenant_id — derive_tenant_uuid can't be reversed.
    # Gated to Slack platform principals; CLI principals never own this row.
    slack_user_tokens_count = 0
    if isinstance(principal, PlatformPrincipalRow) and principal.platform == "slack":
        tenant = await tenants_store.get_tenant(session, principal.tenant_id)
        if tenant is not None:
            slack_user_tokens_count = await slack_user_tokens_store.delete_slack_user_token(
                session, team_id=tenant.external_id, slack_user_id=principal.external_id
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
        slack_user_tokens=slack_user_tokens_count,
        credential_requests=credential_requests_count,
        wizard_sessions=wizard_sessions_count,
        message_feedback=message_feedback_count,
        support_escalations=support_escalations_count,
    )


class PrincipalPurgeResult(BaseModel):
    """Return type of purge_principal: DB report paired with upstream session deletion report."""

    model_config = ConfigDict(frozen=True)

    db: PurgeReport
    sessions: SessionDeletionReport = Field(default_factory=SessionDeletionReport)


async def purge_principal(
    *,
    sm: async_sessionmaker[AsyncSession],
    principal_id: uuid.UUID,
    kind: Literal["cli", "platform"],
    anthropic: AsyncAnthropic | None = None,
) -> PrincipalPurgeResult:
    """Delete every row for `(principal_id, kind)`. Idempotent on re-run.

    When `anthropic` is provided, attempts upstream hard-deletion of every MA
    session tagged for the principal's account_id, under its tenant_id, AFTER
    the DB transaction commits (the DB purge is never rolled back by an
    upstream failure).
    """
    async with sm() as session, session.begin():
        principal = await identity_store.get_principal_by_id(
            session, principal_id=principal_id, kind=kind
        )
        if principal is None:
            return PrincipalPurgeResult(db=PurgeReport())
        db_report = await _purge_principal_in_session(session, principal=principal)
        # Capture before the `async with` block closes the session.
        tenant_id = principal.tenant_id
        account_id = principal.account_id

    # DB transaction committed. Upstream deletion is best-effort — a single
    # call, since one principal belongs to exactly one tenant/account.
    # Deliberate boundary catch mirroring purge_account: the DB purge has
    # already committed, so an upstream APIError must NOT propagate.
    if anthropic is not None:
        try:
            sessions_report = await delete_sessions_for_account(
                anthropic, tenant_id=tenant_id, account_id=account_id
            )
        except APIError as err:
            log.warning(
                "purge.upstream_sessions_failed",
                principal_id=str(principal_id),
                tenant_id=str(tenant_id),
                account_id=str(account_id),
                error=str(err),
            )
            return PrincipalPurgeResult(
                db=db_report, sessions=SessionDeletionReport(upstream_error=True)
            )
        return PrincipalPurgeResult(db=db_report, sessions=sessions_report)

    return PrincipalPurgeResult(db=db_report)


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
    sessions tagged for `account_id` AFTER the DB transaction commits (
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
        # message_feedback: account-id-keyed OR'd with the account's
        # (tenant, platform-user) keys — a vote cast before the person had an
        # accounts row carries a null account_id and is only reachable via the
        # platform-user key. CLI principals contribute no keys: reactions only
        # ever come from a platform user, so cli_list is not walked here.
        platform_user_keys = [(pp.tenant_id, pp.external_id) for pp in pp_list]
        message_feedback_count = await message_feedback_store.delete_message_feedback_for_account(
            session, account_id=account_id, platform_user_keys=platform_user_keys
        )
        # Same account_id = NULL hazard, same obligation: the note is
        # unsolicited personal text, so an account erasure that missed these
        # rows would leave it behind.
        support_escalations_count = (
            await support_escalation_store.delete_support_escalations_for_account(
                session, account_id=account_id, platform_user_keys=platform_user_keys
            )
        )

        # slack_turn_contexts (D-07): keyed by (tenant_id, account_id), not
        # principal_id. Loop every tenant the account's principals belong to —
        # mirrors the upstream session-deletion loop below.
        slack_turn_contexts_count = 0
        for tenant_id in tenant_ids:
            slack_turn_contexts_count += (
                await slack_turn_contexts_store.delete_turn_contexts_for_account(
                    session, tenant_id=tenant_id, account_id=account_id
                )
            )
        user_cfg_count = await accounts_store.delete_user_config_for_account(
            session, account_id=account_id
        )
        account_count = await accounts_store.delete_account(session, account_id=account_id)
        db_report = report.merge(
            PurgeReport(
                mcp_tokens=mcp_tokens_count,
                message_feedback=message_feedback_count,
                support_escalations=support_escalations_count,
                user_configs=user_cfg_count,
                accounts=account_count,
                slack_turn_contexts=slack_turn_contexts_count,
            )
        )

    # DB transaction committed. Upstream deletion is best-effort,
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
