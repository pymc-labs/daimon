"""Shared env form transitions. Adapters supply live platform facts and receipts.

The policy lock stays inside consume_form_unless_pinned's transaction. The
key-set lock covers the second related-name read and every conditional write.
Discord's savepoint and Slack/Teams' rollback-and-consume sequence are retained.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.agent_pins import consume_form_unless_pinned
from daimon.core.continuity.continuation import record_input_continuation
from daimon.core.credential_requests import CredentialRequestOutcome
from daimon.core.env_file import (
    MEMBER_SECRET_SUFFIX_HINT,
    EnvEntry,
    env_alias_shadowed,
    env_import_collisions,
    env_related_held,
)
from daimon.core.posted_controls import CardState
from daimon.core.stores import credential_requests
from daimon.core.stores.agent_files import (
    list_agent_files,
    lock_agent_keys,
    put_agent_file_if_unchanged,
)
from daimon.core.stores.domain import ChatPlatform, CredentialRequestRow
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
