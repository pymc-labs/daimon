"""Shared credential form transitions. Adapters supply live platform facts and receipts.

The policy lock stays inside consume_form_unless_pinned's transaction. The
key-set lock covers the second related-name read and every conditional write.
Discord's savepoint and Slack/Teams' rollback-and-consume sequence are retained.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import MultiFernet
from daimon.core.agent_pins import consume_form_unless_pinned
from daimon.core.continuity.continuation import record_input_continuation
from daimon.core.continuity.messages import ConfigurationChange, render_change_confirmation
from daimon.core.credential_requests import CredentialRequestOutcome, split_skill_repo_target
from daimon.core.env_file import (
    MEMBER_SECRET_SUFFIX_HINT,
    EnvEntry,
    env_alias_shadowed,
    env_import_collisions,
    env_related_held,
)
from daimon.core.mcp_attach import McpConnectDecision
from daimon.core.mcp_oauth import begin_mcp_oauth_flow
from daimon.core.mcp_token_connect import connect_mcp_server_with_token
from daimon.core.posted_controls import CardState
from daimon.core.stores import credential_requests
from daimon.core.stores.agent_files import (
    list_agent_files,
    lock_agent_keys,
    put_agent_file_if_unchanged,
)
from daimon.core.stores.agent_repo_binding import set_binding
from daimon.core.stores.agent_skill_repo_credentials import set_skill_repo_credential
from daimon.core.stores.domain import (
    ChatPlatform,
    CredentialRequestRow,
    McpOAuthFlowRow,
    RepoAccessProof,
)
from daimon.core.stores.seeded_skills import list_seeded_skill_names
from daimon.core.turn_keys import list_turn_key_names
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def env_name_refusal(name: str, problem: str) -> str:
    """The identical key-name refusal used by all three adapters."""
    if problem == "bad_name":
        return f"{name} is not a valid key name (letters, digits, underscores; not leading digit)."
    if problem == "reserved_name":
        return f"{name} is reserved: it changes how the agent's tools run, so it cannot be a key."
    return (
        f"{name} is not a secret name a member can add. Use a name ending in "
        f"{MEMBER_SECRET_SUFFIX_HINT}. An admin can add identity, account, region "
        "and URL names."
    )


@dataclass(frozen=True)
class EnvSubmitPlan:
    related_before: frozenset[str]
    shadowed: str | None
    refuse_replacement: bool


async def prepare_env_submit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    row: CredentialRequestRow,
    replacement_refused: Callable[[], Awaitable[bool]],
) -> EnvSubmitPlan:
    """Snapshot aliases/family, then re-decide a replacement before taking locks.

    The callback reads the submitter's live platform role and fresh target
    facts. It runs only when the card or an alias requires the replace gate.
    Adapters retain their existing order relative to the key-name role read.
    """
    async with session_factory() as session:
        held = await list_turn_key_names(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    related_before = env_related_held(row.target, held)
    shadowed = env_alias_shadowed(row.target, held) if row.replaces_updated_at is None else None
    refuse = (row.replaces_updated_at is not None or shadowed is not None) and (
        await replacement_refused()
    )
    return EnvSubmitPlan(related_before, shadowed, refuse)


@dataclass(frozen=True)
class EnvSubmitResult:
    consumed: CredentialRequestRow | None
    state: CardState = "applied"
    outcome: CredentialRequestOutcome = "applied"
    queued: bool = False


async def apply_env_submit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    row: CredentialRequestRow,
    agent: BetaManagedAgentsAgent,
    platform: ChatPlatform,
    value: str,
    plan: EnvSubmitPlan,
    now: datetime,
) -> EnvSubmitResult:
    """Consume, lock, recheck, conditionally write, settle and queue atomically."""
    state: CardState = "applied"
    outcome: CredentialRequestOutcome = "applied"
    queued = False
    async with session_factory() as session, session.begin():
        consumed = await consume_form_unless_pinned(session, row=row, agent=agent, now=now)
        if consumed is None:
            return EnvSubmitResult(None)
        await lock_agent_keys(session, tenant_id=consumed.tenant_id, agent_id=consumed.agent_id)
        appeared = (
            env_related_held(
                consumed.target,
                await list_turn_key_names(
                    session, tenant_id=consumed.tenant_id, agent_id=consumed.agent_id
                ),
            )
            != plan.related_before
        )
        if plan.refuse_replacement:
            state, outcome = "refused", "write_failed"
        elif appeared:
            state, outcome = "superseded", "stale_replacement"
        else:
            written = await put_agent_file_if_unchanged(
                session,
                tenant_id=consumed.tenant_id,
                agent_id=consumed.agent_id,
                key=consumed.target,
                content=value,
                set_by_account_id=consumed.account_id,
                expected_updated_at=consumed.replaces_updated_at,
            )
            if written is None:
                state, outcome = "superseded", "stale_replacement"
            elif platform == "discord":
                queued = await record_input_continuation(session, consumed, platform=platform)
        await credential_requests.set_credential_request_outcome(
            session, token=consumed.token, outcome=outcome
        )
        if platform != "discord" and state == "applied":
            queued = await record_input_continuation(session, consumed, platform=platform)
    return EnvSubmitResult(consumed, state, outcome, queued)


class _KeyAppearedMidWrite(Exception):
    def __init__(self, entry: EnvEntry) -> None:
        super().__init__(entry.name)
        self.entry = entry


async def _write_entries(
    session: AsyncSession, consumed: CredentialRequestRow, entries: Sequence[EnvEntry]
) -> None:
    for entry in entries:
        written = await put_agent_file_if_unchanged(
            session,
            tenant_id=consumed.tenant_id,
            agent_id=consumed.agent_id,
            key=entry.name,
            content=entry.value,
            set_by_account_id=consumed.account_id,
            expected_updated_at=None,
        )
        if written is None:
            raise _KeyAppearedMidWrite(entry)


async def apply_env_file_submit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    row: CredentialRequestRow,
    agent: BetaManagedAgentsAgent,
    platform: ChatPlatform,
    entries: Sequence[EnvEntry],
    now: datetime,
) -> tuple[CredentialRequestRow | None, tuple[EnvEntry, ...], bool, frozenset[str]]:
    """Whole-file import; collisions spend the request and store no entries.

    Preserve the existing per-platform late-collision rollback boundaries.
    Exceptions propagate to the adapter's existing error/audit boundary.
    """
    try:
        async with session_factory() as session, session.begin():
            consumed = await consume_form_unless_pinned(session, row=row, agent=agent, now=now)
            if consumed is None:
                return None, (), False, frozenset()
            await lock_agent_keys(session, tenant_id=consumed.tenant_id, agent_id=consumed.agent_id)
            held = frozenset(
                file.key
                for file in await list_agent_files(
                    session, tenant_id=consumed.tenant_id, agent_id=consumed.agent_id
                )
            )
            collisions = env_import_collisions(entries, held)
            if not collisions:
                if platform == "discord":
                    try:
                        async with session.begin_nested():
                            await _write_entries(session, consumed, entries)
                    except _KeyAppearedMidWrite as err:
                        collisions = (err.entry,)
                        held = held | {err.entry.name}
                else:
                    await _write_entries(session, consumed, entries)
            queued = False
            if not collisions and platform == "discord":
                queued = await record_input_continuation(session, consumed, platform=platform)
            await credential_requests.set_credential_request_outcome(
                session,
                token=consumed.token,
                outcome="stale_replacement" if collisions else "applied",
            )
            if not collisions and platform != "discord":
                queued = await record_input_continuation(session, consumed, platform=platform)
            return consumed, collisions, queued, held if collisions else frozenset()
    except _KeyAppearedMidWrite as err:
        # Slack/Teams rolled back the first consume along with the file writes.
        async with session_factory() as session, session.begin():
            consumed = await consume_form_unless_pinned(session, row=row, agent=agent, now=now)
            if consumed is None:
                return None, (), False, frozenset()
            await credential_requests.set_credential_request_outcome(
                session, token=row.token, outcome="stale_replacement"
            )
            return consumed, (err.entry,), False, frozenset({err.entry.name})


async def consume_credential_submit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    row: CredentialRequestRow,
    agent: BetaManagedAgentsAgent | None,
    now: datetime,
) -> CredentialRequestRow | None:
    """Spend an external-write form, holding the policy lock through commit."""
    async with session_factory() as session, session.begin():
        return await consume_form_unless_pinned(session, row=row, agent=agent, now=now)


async def settle_credential_submit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    row: CredentialRequestRow,
    platform: ChatPlatform,
    outcome: CredentialRequestOutcome,
    carries_work: bool = True,
    record: Callable[..., Awaitable[bool]] = record_input_continuation,
) -> bool:
    """Commit the external write's outcome and continuation in one transaction."""
    async with session_factory() as session, session.begin():
        await credential_requests.set_credential_request_outcome(
            session, token=row.token, outcome=outcome
        )
        attempt = _ACTIVE_SAVE_ATTEMPT.get()
        if (
            not carries_work
            and attempt is not None
            and attempt.active
            and attempt.token == row.token
            and attempt.retry_allowed
        ):
            # The failure outcome is the audit. A save-only continuation would
            # occupy the retry's idempotency key without resuming its work.
            return False
        return await record(session, row, platform=platform, carries_work=carries_work)


async def begin_oauth_submit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    row: CredentialRequestRow,
    agent: BetaManagedAgentsAgent | None,
    app_root_url: str,
    now: datetime,
) -> tuple[CredentialRequestRow | None, McpOAuthFlowRow | None]:
    """Consume and mint atomically; completion's vault/MCP pin checks stay there."""
    async with session_factory() as session, session.begin():
        consumed = await consume_form_unless_pinned(session, row=row, agent=agent, now=now)
        flow = (
            await begin_mcp_oauth_flow(
                session, request=consumed, app_root_url=app_root_url, now=now
            )
            if consumed is not None
            else None
        )
        return consumed, flow


async def write_mcp_submit(
    client: AsyncAnthropic,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    row: CredentialRequestRow,
    fernet: MultiFernet | None,
    value: str,
    replace_allowed: bool,
    jwt_secret: bytes,
    public_url: str,
    now: datetime,
    connect: Callable[..., Awaitable[None]] = connect_mcp_server_with_token,
) -> None:
    """Attach, publish, then vault-write after the adapter's token probe.

    The connector retains its lock and fresh replacement checks; exceptions
    reach the same platform audit/receipt boundary. The callback retains the
    adapter's connector port.
    """
    assert row.mcp_server_url is not None
    await connect(
        client,
        sessionmaker=session_factory,
        fernet=fernet,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        account_id=row.account_id,
        server_name=row.target,
        mcp_server_url=row.mcp_server_url,
        token=value,
        replace_allowed=replace_allowed,
        jwt_secret=jwt_secret,
        public_url=public_url,
        now=now,
    )


async def write_repo_submit(
    session: AsyncSession,
    *,
    row: CredentialRequestRow,
    ma_secret_ref: str,
    proof: RepoAccessProof,
    write: Callable[..., Awaitable[object]] = set_binding,
) -> None:
    """Write the working repo promised by the card, inside the caller's transaction."""
    url, branch, _path = split_skill_repo_target(row.target)
    await write(
        session,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        repo_url=url,
        default_branch=branch,
        ma_secret_ref=ma_secret_ref,
        proof=proof,
    )


async def write_skill_repo_submit(
    session: AsyncSession,
    *,
    row: CredentialRequestRow,
    ma_secret_ref: str,
    proof: RepoAccessProof,
) -> frozenset[str]:
    """Store the skill repo credential and read the seeded-name fence together."""
    url, branch, path = split_skill_repo_target(row.target)
    await set_skill_repo_credential(
        session,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        repo_url=url,
        default_branch=branch,
        path=path,
        ma_secret_ref=ma_secret_ref,
        proof=proof,
    )
    return await list_seeded_skill_names(session, tenant_id=row.tenant_id)


async def prepare_mcp_submit(
    *,
    decide: Callable[[], Awaitable[McpConnectDecision]],
    clock: Callable[[], datetime],
) -> tuple[McpConnectDecision, datetime]:
    """Snapshot the live platform replacement decision before opening the consume.

    Adapters inject their decision helper unchanged. A refused replacement
    still spends the form and receives the platform's existing receipt.
    """
    decision = await decide()
    return decision, clock()


CREDENTIAL_SAVE_TIMEOUT_SECONDS = 90.0


@dataclass
class CredentialSaveAttempt:
    """A policy refusal remains terminal even inside a guarded external save."""

    token: str
    active: bool = True
    retry_allowed: bool = True
    failure_reason: str | None = None


_ACTIVE_SAVE_ATTEMPT: ContextVar[CredentialSaveAttempt | None] = ContextVar(
    "credential_save_attempt", default=None
)


def note_credential_save_failure(
    row: CredentialRequestRow, outcome: ConfigurationChange | None
) -> None:
    """Keep the adapter's safe partial-progress receipt on its retry card."""
    attempt = _ACTIVE_SAVE_ATTEMPT.get()
    if (
        attempt is not None
        and attempt.active
        and attempt.token == row.token
        and outcome is not None
    ):
        attempt.failure_reason = (
            f"{render_change_confirmation(outcome)}\nTry again using the same form."
        )


@asynccontextmanager
async def guard_credential_save(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    row: CredentialRequestRow,
    edit_retry: Callable[[CredentialRequestRow, str], Awaitable[None]],
    timeout_seconds: float | None = None,
) -> AsyncIterator[CredentialSaveAttempt]:
    """Bound the save and repair every failed/unfinished external-write receipt.

    Cancellation stops this attempt before its consume is released. A retry
    rechecks all live authorization and replacement gates. Public reasons
    are fixed strings; exceptions can contain submitted secrets.
    """
    attempt = CredentialSaveAttempt(token=row.token)
    active_token = _ACTIVE_SAVE_ATTEMPT.set(attempt)
    reason = "Saving did not finish; some changes may have been saved. Try again."
    cancelled = False
    try:
        async with asyncio.timeout(
            CREDENTIAL_SAVE_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        ):
            yield attempt
    except TimeoutError:
        reason = "Saving took too long; some changes may have been saved. Try again."
    except asyncio.CancelledError:
        cancelled = True
    except Exception as err:
        structlog.get_logger().warning(
            "credential_submit.save_failed", kind=row.kind, err_type=type(err).__name__
        )
    finally:
        attempt.active = False
        _ACTIVE_SAVE_ATTEMPT.reset(active_token)
        async with session_factory() as session, session.begin():
            current = await credential_requests.peek_credential_request(session, token=row.token)
            retry = None
            if current is not None and (attempt.retry_allowed or current.outcome is None):
                outcome = current.outcome
                if outcome in (None, "token_rejected", "write_failed"):
                    reason = attempt.failure_reason or reason
                    if outcome == "token_rejected":
                        reason = (
                            "That token was rejected: it cannot access the requested service. "
                            "Try again."
                        )
                    retry = await credential_requests.release_failed_credential_request(
                        session, row=row, outcome=outcome or "write_failed"
                    )
        if retry is not None:
            await edit_retry(retry, reason)
    if cancelled:
        raise asyncio.CancelledError
