"""GDPR purge orchestrator.

Single core entry point that deletes every row referencing a principal (or all
principals under an account) across stores, in FK-safe order, inside a single
transaction. Idempotent on re-run; rolls back fully on any helper raise.

Registry note: the tables and their FK-safe order live in `daimon.core.erasure.ERASABLE`,
which the privacy preview counts from too. Each principal runs the principal pass; the
account pass runs after every principal and before the account row goes.

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

Long-job records are erased through the accounts.id ON DELETE CASCADE; their
cancellation audit rows cascade from the job. Worker sessions carry the same
account/tenant stamps as other owned sessions and use the existing upstream
session-deletion work item. No separate retained job-content exception applies.

Fly registrations, their conversation metadata and background command rows
(including stored output tails) cascade on account deletion.
The compute reconciler deletes the remaining tagged apps as unregistered orphans;
upstream machine/file deletion requires the scheduler and Fly to be available.

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
  `spend_allowances` rows belong to the tenant's configuration like the caps: they name a
  person only by platform user id, and erasure nulls `created_by_account_id` of the admin
  who set one, through ON DELETE SET NULL.
  `promo_redemptions` rows stay with the tenant too; account deletion only
  clears who redeemed the code, through ON DELETE SET NULL.
- Uploaded MA skill files (user_skills rows) are our DB ledger: the DB row is
  deleted as part of purge, but the uploaded file content inside Anthropic's
  Managed Agents is retained — guild-shared agents may still reference the
  underlying skill and the MA workspace is not under our delete authority.
  A skill added by hand is keyed by its agent, not the person who added it;
  erasure only nulls its `added_by_account_id`, through ON DELETE SET NULL.
- A skill mirrored from a skills server stays with the tenant; erasure nulls its
  `added_by_account_id` (ON DELETE SET NULL) and the mirror stops updating.
- A conversation's chosen model (`thread_model_settings`) and each agent's consent to it
  (`thread_model_consents`) belong to the tenant. They name no person, only the thread and
  agent, so account erasure leaves them and they go when the install does (ON DELETE CASCADE).
- `workspace_members` rows are kept: they mirror who the chat platform lists as a
  member (platform user id and admin flag, no content), and the next reload would
  restore a deleted row. They go when the install ends.
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
from collections.abc import Iterable
from dataclasses import fields, replace
from typing import Literal

import structlog
from anthropic import APIError, AsyncAnthropic
from daimon.core import platform_names
from daimon.core.anthropic_guard import anthropic_configured
from daimon.core.channel_backend import backend_refusal_causes
from daimon.core.erasure import ERASABLE, Principal, Subject
from daimon.core.errors import UserFacingError
from daimon.core.ma import SessionDeletionReport, delete_sessions_for_account
from daimon.core.memory_resource import (
    delete_personal_memory_stores_for_account,
    sweep_erased_personal_memory,
)
from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import github_issued_tokens as github_issued_tokens_store
from daimon.core.stores import github_links as github_links_store
from daimon.core.stores import github_router_sessions as github_router_sessions_store
from daimon.core.stores import identity as identity_store
from daimon.core.stores import mux_erasure as mux_erasure_store
from daimon.core.stores import privacy_session_deletes as privacy_deletes_store
from daimon.core.stores import routines as routines_store
from daimon.core.stores import security_audit as security_audit_store
from daimon.core.stores import watches as watches_store
from daimon.core.stores.domain import PlatformPrincipalRow
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


class PurgeReport(BaseModel):
    """Per-table rowcount summary returned by purge entry points."""

    model_config = ConfigDict(frozen=True)

    watches: int = 0
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
    connector_tokens: int = 0
    gemini_records: int = 0
    selfhosted_operations: int = 0
    slack_user_tokens: int = 0
    slack_turn_contexts: int = 0
    direct_message_conversations: int = 0
    dm_carry_overs: int = 0
    held_timers: int = 0
    tag_opt_outs: int = 0
    dm_picks: int = 0
    dm_rules: int = 0
    dm_grant_users: int = 0
    credential_requests: int = 0
    wizard_sessions: int = 0
    message_feedback: int = 0
    support_escalations: int = 0
    # Plan rows keep their charge with the person erased (decision 7).
    support_requests_anonymized: int = 0
    support_person_limits: int = 0
    # Requests the person settled as staff: who settled is cleared, the charge kept.
    support_settlers_cleared: int = 0
    daimon_admins: int = 0
    agent_post_requesters: int = 0
    platform_user_names: int = 0
    teams_messages: int = 0
    microsoft_user_tokens: int = 0
    lead_intakes: int = 0
    web_people: int = 0
    web_admin_grants: int = 0
    # The account's private provider slots and what hangs off them (stores.mux_erasure).
    mux_slots: int = 0
    mux_binding_generations: int = 0
    mux_journal_sessions: int = 0
    mux_journal_events: int = 0
    mux_leases: int = 0
    mux_release_requests: int = 0
    mux_operations: int = 0
    host_call_cards: int = 0

    def merge(self, other: PurgeReport) -> PurgeReport:
        return PurgeReport.model_validate(
            {name: getattr(self, name) + getattr(other, name) for name in PurgeReport.model_fields}
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


async def _purge_principal_in_session(
    session: AsyncSession, *, principal: Principal
) -> PurgeReport:
    """Delete every row referencing `principal` on the given open session, in `ERASABLE` order.

    Does NOT open a transaction — caller owns the begin() block. Failures
    propagate so the caller's transaction rolls back.
    """
    return PurgeReport.model_validate(
        {e.key: await e.principal(session, principal) for e in ERASABLE if e.principal}
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
        queued_names = _queued_name_keys([principal])
    platform_names.forget_users(queued_names)

    # DB transaction committed. Upstream deletion is best-effort — a single
    # call, since one principal belongs to exactly one tenant/account.
    # Deliberate boundary catch mirroring purge_account: the DB purge has
    # already committed, so an upstream APIError must NOT propagate. With no
    # Anthropic key there are no Managed Agents sessions to delete.
    if anthropic is not None and anthropic_configured(anthropic):
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
                causes=backend_refusal_causes(err),
            )
            return PrincipalPurgeResult(
                db=db_report, sessions=SessionDeletionReport(upstream_error=True)
            )
        return PrincipalPurgeResult(db=db_report, sessions=sessions_report)

    return PrincipalPurgeResult(db=db_report)


def _queued_name_keys(principals: Iterable[Principal]) -> list[platform_names.UserKey]:
    """The (tenant, platform, user) keys whose queued display names a purge drops."""
    return [
        (principal.tenant_id, principal.platform, principal.external_id)
        for principal in principals
        if isinstance(principal, PlatformPrincipalRow)
    ]


async def _purge_account_transaction(
    session: AsyncSession, *, account_id: uuid.UUID
) -> tuple[PurgeReport, mux_erasure_store.MuxRetained, list[platform_names.UserKey]]:
    """Everything `purge_account` deletes locally, in the caller's transaction.

    Takes the account lock, then the erasure locks (see `stores.mux_erasure` for the lock
    order).
    """
    await privacy_deletes_store.lock_account(session, account_id=account_id)
    watches_count = await watches_store.delete_for_account(session, account_id=account_id)
    await security_audit_store.erase_account_for_privacy(session, account_id=account_id)
    cli_list = await identity_store.list_cli_principals_for_account(session, account_id=account_id)
    pp_list = await identity_store.list_platform_principals_for_account(
        session, account_id=account_id
    )
    # The erasure locks, after the account lock above: from here no mux writer of this
    # account or its principals can start, and those already running have finished.
    await mux_erasure_store.lock_for_erasure(
        session,
        account_id=account_id,
        principal_ids=[principal.id for principal in (*cli_list, *pp_list)],
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
    # Who set a routine's sandbox opt-in: keep the change, drop the person.
    await routines_store.erase_sandbox_setter(
        session,
        ids=[str(account_id), *(str(principal.id) for principal in cli_list)],
        platform_users=[(pp.tenant_id, pp.external_id) for pp in pp_list],
    )
    for principal in (*cli_list, *pp_list):
        sub = await _purge_principal_in_session(session, principal=principal)
        report = report.merge(sub)

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
    # What the principal passes left: account-keyed rows, and rows keyed by any of the
    # account's platform users (a vote cast before the account existed). All of them
    # reference accounts.id, so they go before the account row.
    subject = Subject(account_id, tuple(cli_list), tuple(pp_list))
    counts = {e.key: await e.account(session, subject) for e in ERASABLE if e.account}
    await github_issued_tokens_store.erase_requester_identity(session, account_id=account_id)
    # Revoke the person's GitHub router bearers before SET NULL detaches them.
    await github_router_sessions_store.close_for_account(session, account_id=account_id)
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
    mux_counts = {f"mux_{f.name}": getattr(mux_erased, f.name) for f in fields(mux_erased)}
    db_report = report.merge(
        PurgeReport.model_validate(
            {
                **counts,
                **mux_counts,
                "watches": watches_count,
                "accounts": account_count,
                "github_user_links": github_user_links_count,
            }
        )
    )
    return db_report, mux_retained, _queued_name_keys((*cli_list, *pp_list))


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
) -> tuple[PurgeReport, mux_erasure_store.MuxRetained, list[platform_names.UserKey]]:
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
    db_report, mux_retained, queued_names = await _purge_account_with_retries(
        sm, account_id=account_id
    )
    platform_names.forget_users(queued_names)

    # DB transaction committed. Upstream deletion is best-effort,
    # looped over every tenant the account's principals belonged to.
    # Deliberate boundary catch: the DB purge has already committed, so an
    # upstream APIError must NOT propagate — the caller would misreport a
    # completed, irreversible erasure as failed. Fold the failure into the
    # sessions report instead (upstream_error=True) and log for the operator;
    # a failure in one tenant does not skip the remaining tenants. With no
    # Anthropic key the work item stays queued for an install that adds one.
    if anthropic is not None and anthropic_configured(anthropic):
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
                causes=backend_refusal_causes(err),
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
    left for it. With no Anthropic key the queued work waits for one.
    """
    configured = anthropic_configured(anthropic)
    if configured:
        try:
            await sweep_erased_personal_memory(anthropic, sm)
        except Exception:
            log.exception("purge.personal_memory_sweep_failed")
    async with sm() as session:
        work = await privacy_deletes_store.list_work(session)
    for item in work:
        if not configured and item.tenant_ids:
            continue
        try:
            await retry_account_session_delete(anthropic, sm, account_id=item.account_id)
        except Exception:
            log.exception("purge.queued_upstream_sessions_failed", account_id=str(item.account_id))
