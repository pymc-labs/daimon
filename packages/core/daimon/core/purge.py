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
principal_links -> principal row -> platform_user_names (platform only,
platform-user-scoped, after the principal row on purpose: see the call) ->
teams_messages and microsoft_user_tokens (Teams principals only, keyed by (tenant,
Entra user)).
Account-level deletes (mcp_tokens, GitHub connect requests and click intents,
message_feedback, user_configs, accounts)
run in `purge_account` after all principal rows are gone; mcp_tokens and
message_feedback are keyed by account_id and are deleted before delete_account
so their CASCADE FKs to accounts.id are satisfied. message_feedback is deleted
by an account-id predicate OR'd with the account's (tenant, platform-user)
keys, because a vote cast before the person had an account row carries a null
account id.

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
Protocol.

Deliberate carve-outs:
- Provider-neutral (mux) state: a person's private slots, with their binding generations,
  journal, leases, operations and release requests, are erased with the account, and the
  account is fenced so no retained scope or lease writes again (`stores.mux_erasure`).
  Shared slots and the operations a person ran against them stay, as do the usage
  observations and accounting outbox rows of the erased slots: the tenant still settles
  from them. Purge reports them as retained rather than removing them.
- Shared setup conversations retain their binding and other participants' work;
  account deletion removes creator attribution through ON DELETE SET NULL.
  Short-lived turn origins are removed through the account's ON DELETE CASCADE,
  including any still-active authority; neither needs an explicit delete helper.
- Hosted chart artifacts in operator-owned object storage are not deleted by
  account purge; operators must enforce retention with a bucket lifecycle rule.
- `usage_events` and `tenant_user_caps` rows are retained for billing integrity.
  A `channel_budgets` row belongs to the tenant; erasure only nulls the
  `set_by_account_id` of the admin who set it, through ON DELETE SET NULL.
  Their `delete_all_for_user` helpers exist and are deliberately uncalled here.
  `promo_redemptions` rows stay with the tenant too; account deletion only
  clears who redeemed the code, through ON DELETE SET NULL.
- Uploaded MA skill files (user_skills rows) are our DB ledger: the DB row is
  deleted as part of purge, but the uploaded file content inside Anthropic's
  Managed Agents is retained — guild-shared agents may still reference the
  underlying skill and the MA workspace is not under our delete authority.
  A skill added by hand is keyed by its agent, not the person who added it;
  erasure only nulls its `added_by_account_id`, through ON DELETE SET NULL.
- The user's GitHub-side OAuth grant is not revoked. No GitHub API client enters
  the purge path — we delete only our encrypted credential and oauth-state rows
  from our own DB.
- An agent's shared memory store is agent-scoped and shared across all
  workspace users, so account purge does NOT touch it — archiving it because a
  single member invoked erasure would destroy the guild's shared agent memory.
  It may retain information about the purged user. The person's own personal
  memory stores (one per agent they talked to, archived ones included) ARE
  deleted upstream after the DB transaction commits; their bindings carry no
  FK to `accounts`, so they survive the account row as the record of what is
  left, and `sweep_privacy_session_deletes` finishes any delete that failed or
  never ran.
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

import asyncio
import random
import uuid
from dataclasses import replace
from typing import Literal

import structlog
from anthropic import APIError, AsyncAnthropic
from daimon.core import platform_names
from daimon.core.errors import UserFacingError
from daimon.core.ma import SessionDeletionReport, delete_sessions_for_account
from daimon.core.memory_resource import (
    delete_personal_memory_stores_for_account,
    sweep_erased_personal_memory,
)
from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import agent_posts as agent_posts_store
from daimon.core.stores import channel_admins as channel_admins_store
from daimon.core.stores import credential_requests as credential_requests_store
from daimon.core.stores import direct_messages as direct_messages_store
from daimon.core.stores import gemini_runtime_state as gemini_state_store
from daimon.core.stores import github_connect as github_connect_store
from daimon.core.stores import github_issued_tokens as github_issued_tokens_store
from daimon.core.stores import github_links as github_links_store
from daimon.core.stores import identity as identity_store
from daimon.core.stores import mcp_tokens as mcp_tokens_store
from daimon.core.stores import message_feedback as message_feedback_store
from daimon.core.stores import microsoft_user_tokens as microsoft_tokens_store
from daimon.core.stores import mux_erasure as mux_erasure_store
from daimon.core.stores import platform_names as platform_names_store
from daimon.core.stores import privacy_session_deletes as privacy_deletes_store
from daimon.core.stores import routines as routines_store
from daimon.core.stores import security_audit as security_audit_store
from daimon.core.stores import selfhosted_resources as selfhosted_store
from daimon.core.stores import slack_turn_contexts as slack_turn_contexts_store
from daimon.core.stores import slack_user_tokens as slack_user_tokens_store
from daimon.core.stores import support_escalation as support_escalation_store
from daimon.core.stores import teams_message_admissions as teams_admissions_store
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import user_skills as user_skills_store
from daimon.core.stores import wizard_session as wizard_session_store
from daimon.core.stores.domain import CliPrincipalRow, PlatformPrincipalRow
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
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
    github_connect_requests: int = 0
    github_connect_click_intents: int = 0
    github_user_links: int = 0
    mcp_tokens: int = 0
    gemini_records: int = 0
    selfhosted_operations: int = 0
    slack_user_tokens: int = 0
    slack_turn_contexts: int = 0
    direct_message_conversations: int = 0
    credential_requests: int = 0
    wizard_sessions: int = 0
    message_feedback: int = 0
    support_escalations: int = 0
    channel_admins: int = 0
    agent_post_requesters: int = 0
    platform_user_names: int = 0
    teams_messages: int = 0
    microsoft_user_tokens: int = 0
    # The account's private provider slots and what hangs off them (stores.mux_erasure).
    mux_slots: int = 0
    mux_binding_generations: int = 0
    mux_journal_sessions: int = 0
    mux_journal_events: int = 0
    mux_leases: int = 0
    mux_release_requests: int = 0
    mux_operations: int = 0

    def merge(self, other: PurgeReport) -> PurgeReport:
        return PurgeReport(
            routines=self.routines + other.routines,
            principal_links=self.principal_links + other.principal_links,
            cli_principals=self.cli_principals + other.cli_principals,
            platform_principals=self.platform_principals + other.platform_principals,
            user_configs=self.user_configs + other.user_configs,
            accounts=self.accounts + other.accounts,
            user_skills=self.user_skills + other.user_skills,
            github_connect_requests=self.github_connect_requests + other.github_connect_requests,
            github_connect_click_intents=(
                self.github_connect_click_intents + other.github_connect_click_intents
            ),
            github_user_links=self.github_user_links + other.github_user_links,
            mcp_tokens=self.mcp_tokens + other.mcp_tokens,
            gemini_records=self.gemini_records + other.gemini_records,
            selfhosted_operations=self.selfhosted_operations + other.selfhosted_operations,
            slack_user_tokens=self.slack_user_tokens + other.slack_user_tokens,
            slack_turn_contexts=self.slack_turn_contexts + other.slack_turn_contexts,
            direct_message_conversations=(
                self.direct_message_conversations + other.direct_message_conversations
            ),
            credential_requests=self.credential_requests + other.credential_requests,
            wizard_sessions=self.wizard_sessions + other.wizard_sessions,
            message_feedback=self.message_feedback + other.message_feedback,
            support_escalations=self.support_escalations + other.support_escalations,
            channel_admins=self.channel_admins + other.channel_admins,
            agent_post_requesters=self.agent_post_requesters + other.agent_post_requesters,
            platform_user_names=self.platform_user_names + other.platform_user_names,
            teams_messages=self.teams_messages + other.teams_messages,
            microsoft_user_tokens=self.microsoft_user_tokens + other.microsoft_user_tokens,
            mux_slots=self.mux_slots + other.mux_slots,
            mux_binding_generations=self.mux_binding_generations + other.mux_binding_generations,
            mux_journal_sessions=self.mux_journal_sessions + other.mux_journal_sessions,
            mux_journal_events=self.mux_journal_events + other.mux_journal_events,
            mux_leases=self.mux_leases + other.mux_leases,
            mux_release_requests=self.mux_release_requests + other.mux_release_requests,
            mux_operations=self.mux_operations + other.mux_operations,
        )


class AccountPurgeResult(BaseModel):
    """Return type of purge_account: DB report paired with upstream session deletion report."""

    model_config = ConfigDict(frozen=True)

    db: PurgeReport
    sessions: SessionDeletionReport = Field(default_factory=SessionDeletionReport)
    # What the account's erasure left in the mux tables: shared conversations and
    # settlement evidence (see `daimon.core.stores.mux_erasure`).
    mux_retained: mux_erasure_store.MuxRetained = Field(
        default_factory=mux_erasure_store.MuxRetained
    )


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
    agent_post_requesters_count = 0
    platform_user_names_count = 0
    teams_messages_count = 0
    microsoft_user_tokens_count = 0
    if isinstance(principal, PlatformPrincipalRow):
        routines_count = await routines_store.delete_for_principal(
            session,
            tenant_id=principal.tenant_id,
            external_id=principal.external_id,
        )
        kind: Literal["cli", "platform"] = "platform"
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
        # A channel admin grant names the person by platform user id; the row
        # itself is the tenant's, so only their id leaves it.
        channel_admins_count = await channel_admins_store.remove_user_from_channel_admins(
            session,
            tenant_id=principal.tenant_id,
            platform=principal.platform,
            platform_user_id=principal.external_id,
        )
        # A turn's posts and an auto-opened thread name who asked; the posts
        # are the agent's, so only the person's id leaves them.
        agent_post_requesters_count = await agent_posts_store.clear_requester(
            session,
            tenant_id=principal.tenant_id,
            platform=principal.platform,
            platform_user_id=principal.external_id,
        )
    else:
        routines_count = 0
        kind = "cli"
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
        # flow is chat-platform-only), so this always reports 0 — kept for
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
        channel_admins_count = 0

    user_skills_count = await user_skills_store.delete_user_skills_for_principal(
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
    if isinstance(principal, PlatformPrincipalRow):
        # The name the platform last gave for them (the billing panel's
        # fallback), keyed by (tenant, platform, platform user) like the grants
        # above. Deleted AFTER the principal row: a name write locks that row,
        # so one racing this purge has either committed (and is deleted here)
        # or will find no principal and store nothing. The in-process queue is
        # dropped too, so a queued write cannot follow.
        user_key = (principal.tenant_id, principal.platform, principal.external_id)
        platform_names.forget_users([user_key])
        platform_user_names_count = await platform_names_store.delete_user_names_for_platform_user(
            session,
            tenant_id=principal.tenant_id,
            platform=principal.platform,
            platform_user_id=principal.external_id,
        )
        if principal.platform == "teams":
            # Their message receipts lose the person and any unhandled words but stay as the
            # duplicate guard: deleted, a later catch-up would run the message again.
            teams_messages_count = await teams_admissions_store.forget_teams_messages_for_user(
                session, tenant_id=principal.tenant_id, user_id=principal.external_id
            )
            # Their Connect Microsoft refresh token; sign-in links cascade with the account.
            microsoft_user_tokens_count = await microsoft_tokens_store.delete_microsoft_user_token(
                session, tenant_id=principal.tenant_id, user_id=principal.external_id
            )

    return PurgeReport(
        routines=routines_count,
        principal_links=links_count,
        cli_principals=principal_count if kind == "cli" else 0,
        platform_principals=principal_count if kind == "platform" else 0,
        user_skills=user_skills_count,
        slack_user_tokens=slack_user_tokens_count,
        credential_requests=credential_requests_count,
        wizard_sessions=wizard_sessions_count,
        message_feedback=message_feedback_count,
        support_escalations=support_escalations_count,
        channel_admins=channel_admins_count,
        agent_post_requesters=agent_post_requesters_count,
        platform_user_names=platform_user_names_count,
        teams_messages=teams_messages_count,
        microsoft_user_tokens=microsoft_user_tokens_count,
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


async def _purge_account_transaction(
    session: AsyncSession, *, account_id: uuid.UUID
) -> tuple[PurgeReport, mux_erasure_store.MuxRetained]:
    """Everything `purge_account` deletes locally, in the caller's transaction.

    Takes the account's exclusive lock first (see `stores.mux_erasure` for the lock order).
    """
    await privacy_deletes_store.lock_account(session, account_id=account_id)
    await security_audit_store.erase_account_for_privacy(session, account_id=account_id)
    cli_list = await identity_store.list_cli_principals_for_account(session, account_id=account_id)
    pp_list = await identity_store.list_platform_principals_for_account(
        session, account_id=account_id
    )
    # Capture every distinct tenant before the session closes — each
    # tenant's agents must be enumerated for upstream session deletion.
    tenant_ids: set[uuid.UUID] = {principal.tenant_id for principal in (*cli_list, *pp_list)}
    if tenant_ids:
        await privacy_deletes_store.ensure_work(
            session, account_id=account_id, tenant_ids=tenant_ids
        )

    report = PurgeReport()
    github_account_link = await github_links_store.get_account_link(session, account_id=account_id)
    for principal in (*cli_list, *pp_list):
        sub = await _purge_principal_in_session(session, principal=principal)
        report = report.merge(sub)

    # Conversation/artifact records are purged. Conservative holds contain
    # financial attempt metadata, not conversation payload; retain them as
    # audit evidence rather than replenishing a cap when an account is erased.
    # The account's private provider slots, journal, leases, operations and release
    # requests. Shared conversations and settlement evidence stay (the retained
    # counts say so). Before the account row goes: operations are also matched by
    # the principals gathered above.
    principal_ids = [principal.id for principal in (*cli_list, *pp_list)]
    mux_retained = (
        await mux_erasure_store.inventory(
            session, account_id=account_id, principal_ids=principal_ids
        )
    ).retained
    mux_erased = await mux_erasure_store.erase(
        session, account_id=account_id, principal_ids=principal_ids
    )
    gemini_records_count = await gemini_state_store.delete_records_for_account(
        session, account_id=account_id
    )
    selfhosted_operations_count = await selfhosted_store.delete_operations_for_account(
        session, account_id=account_id
    )
    # Account-scoped (keyed by account_id, not principal): delete the
    # per-agent MCP token rows BEFORE delete_account — they reference
    # accounts.id, so leaving them trips the FK and rolls back the purge.
    mcp_tokens_count = await mcp_tokens_store.delete_tokens_for_account(
        session, account_id=account_id
    )
    github_connect_requests_count = await github_connect_store.delete_requests_for_account(
        session, account_id=account_id
    )
    github_connect_click_intents_count = (
        await github_connect_store.delete_click_intents_for_account(session, account_id=account_id)
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

    # slack_turn_contexts: keyed by (tenant_id, account_id), not
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
    direct_message_count = await direct_messages_store.delete_conversations_for_account(
        session, account_id=account_id
    )
    await github_issued_tokens_store.erase_requester_identity(session, account_id=account_id)
    account_count = await accounts_store.delete_account(session, account_id=account_id)
    github_user_links_count = (
        await github_links_store.delete_unlinked_user(
            session, github_user_id=github_account_link.github_user_id
        )
        if github_account_link is not None and account_count
        else 0
    )
    if account_count or mux_erased.total or mux_retained.total:
        await mux_erasure_store.mark_erased(
            session, account_id=account_id, principal_ids=principal_ids
        )
    db_report = report.merge(
        PurgeReport(
            mux_slots=mux_erased.slots,
            mux_binding_generations=mux_erased.binding_generations,
            mux_journal_sessions=mux_erased.journal_sessions,
            mux_journal_events=mux_erased.journal_events,
            mux_leases=mux_erased.leases,
            mux_release_requests=mux_erased.release_requests,
            mux_operations=mux_erased.operations,
            mcp_tokens=mcp_tokens_count,
            gemini_records=gemini_records_count,
            selfhosted_operations=selfhosted_operations_count,
            github_connect_requests=github_connect_requests_count,
            github_connect_click_intents=github_connect_click_intents_count,
            message_feedback=message_feedback_count,
            support_escalations=support_escalations_count,
            user_configs=user_cfg_count,
            accounts=account_count,
            github_user_links=github_user_links_count,
            slack_turn_contexts=slack_turn_contexts_count,
            direct_message_conversations=direct_message_count,
        )
    )
    return db_report, mux_retained


#: A purge that cannot get the account lock, or loses a deadlock, rolls back whole and
#: starts again. Writers hold the account lock for one short transaction, so waiting longer
#: than this means something is stuck, and the person is told the deletion did not happen.
PURGE_LOCK_TIMEOUT = "5s"
PURGE_ATTEMPTS = 5
_RETRYABLE_SQLSTATES = frozenset({"40P01", "55P03"})  # deadlock_detected, lock_not_available


class AccountPurgeFailed(UserFacingError):
    """The purge could not finish and rolled back: nothing was deleted."""


def _retryable(error: DBAPIError) -> bool:
    cause: object = error.orig
    state = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
    return state in _RETRYABLE_SQLSTATES


async def _purge_account_with_retries(
    sm: async_sessionmaker[AsyncSession], *, account_id: uuid.UUID
) -> tuple[PurgeReport, mux_erasure_store.MuxRetained]:
    for attempt in range(1, PURGE_ATTEMPTS + 1):
        try:
            async with sm() as session, session.begin():
                await session.execute(text(f"SET LOCAL lock_timeout = '{PURGE_LOCK_TIMEOUT}'"))
                return await _purge_account_transaction(session, account_id=account_id)
        except DBAPIError as error:
            if not _retryable(error):
                raise
            log.warning(
                "purge.account_retry", account_id=str(account_id), attempt=attempt, error=str(error)
            )
            if attempt == PURGE_ATTEMPTS:
                raise AccountPurgeFailed(
                    "Deleting your account didn't finish, so nothing was deleted. "
                    "Try again in a minute."
                ) from error
            await asyncio.sleep(random.uniform(0.1, 0.4) * attempt)
    raise AssertionError("unreachable")


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

    The local transaction retains the account and all principal tenant IDs in
    a durable MA deletion work item. When `anthropic` is provided, attempts
    upstream hard-deletion after commit; the scheduler retries unfinished work.
    Sessions are enumerated across EVERY linked-principal tenant —
    `principal_links` permits an account to span tenants.
    """
    db_report, mux_retained = await _purge_account_with_retries(sm, account_id=account_id)

    # DB transaction committed. Upstream deletion is best-effort,
    # looped over every tenant the account's principals belonged to.
    # Deliberate boundary catch: the DB purge has already committed, so an
    # upstream APIError must NOT propagate — the caller would misreport a
    # completed, irreversible erasure as failed. Fold the failure into the
    # sessions report instead (upstream_error=True) and log for the operator;
    # a failure in one tenant does not skip the remaining tenants.
    if anthropic is not None:
        sessions = await retry_account_session_delete(anthropic, sm, account_id=account_id)
        # The person's own memory stores, archived ones included: deleted
        # upstream, then unbound. Independent of the session work item, which
        # exists only while the account still had principals; a store that
        # could not be deleted keeps its binding, and
        # `sweep_privacy_session_deletes` finishes it.
        if not await delete_personal_memory_stores_for_account(
            anthropic, sm, account_id=account_id
        ):
            sessions = replace(sessions, upstream_error=True)
        return AccountPurgeResult(db=db_report, sessions=sessions, mux_retained=mux_retained)

    return AccountPurgeResult(db=db_report, mux_retained=mux_retained)


async def retry_account_session_delete(
    anthropic: AsyncAnthropic,
    sm: async_sessionmaker[AsyncSession],
    *,
    account_id: uuid.UUID,
) -> SessionDeletionReport:
    """Retry one durable work item; retain it until all tenant scans succeed."""
    async with sm() as lock_session, lock_session.begin():
        await privacy_deletes_store.lock_account(lock_session, account_id=account_id)
        return await _retry_account_session_delete_locked(anthropic, sm, account_id=account_id)


async def _retry_account_session_delete_locked(
    anthropic: AsyncAnthropic,
    sm: async_sessionmaker[AsyncSession],
    *,
    account_id: uuid.UUID,
) -> SessionDeletionReport:
    async with sm() as session:
        work = await privacy_deletes_store.get_work(session, account_id=account_id)
    if work is None:
        return SessionDeletionReport()

    deleted = 0
    failed = 0
    upstream_error = False
    for tenant_id in work.tenant_ids:

        async def remember(session_ids: set[str], tenant_id: uuid.UUID = tenant_id) -> None:
            async with sm() as session, session.begin():
                await privacy_deletes_store.add_pending(
                    session, account_id=account_id, tenant_id=tenant_id, session_ids=session_ids
                )

        async def forget(session_id: str, tenant_id: uuid.UUID = tenant_id) -> None:
            async with sm() as session, session.begin():
                await privacy_deletes_store.remove_pending(
                    session, account_id=account_id, tenant_id=tenant_id, session_id=session_id
                )

        try:
            sub = await delete_sessions_for_account(
                anthropic,
                tenant_id=tenant_id,
                account_id=account_id,
                pending_session_ids=set(work.pending_session_ids.get(str(tenant_id), [])),
                on_discovered=remember,
                on_deleted=forget,
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

    if not upstream_error and failed == 0:
        async with sm() as session, session.begin():
            current = await privacy_deletes_store.get_work(session, account_id=account_id)
            if current is not None and not any(current.pending_session_ids.values()):
                await privacy_deletes_store.finish_work(session, account_id=account_id)
    return SessionDeletionReport(deleted=deleted, failed=failed, upstream_error=upstream_error)


async def sweep_privacy_session_deletes(
    anthropic: AsyncAnthropic, sm: async_sessionmaker[AsyncSession]
) -> None:
    """Retry queued account erasures, isolating failures by account.

    Also finishes the personal memory stores of every account already erased
    (`sweep_erased_personal_memory`), whether or not a session work item is
    left for it.
    """
    try:
        await sweep_erased_personal_memory(anthropic, sm)
    except Exception:
        log.exception("purge.personal_memory_sweep_failed")
    async with sm() as session:
        work = await privacy_deletes_store.list_work(session)
    for item in work:
        try:
            await retry_account_session_delete(anthropic, sm, account_id=item.account_id)
        except Exception:
            log.exception("purge.queued_upstream_sessions_failed", account_id=str(item.account_id))
