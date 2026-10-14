"""Explicit host evidence adapters for mux C14, C17 and C18.

These probes are never installed by the application. A conformance envelope
supplies its real preparation/driver callbacks and an isolated host database.
The shared mux fixtures judge the returned facts; this module returns no verdict.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TypedDict
from uuid import UUID

from daimon.core._models import TurnOutcome
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.wakes import (
    WAKE_CLAIM_LEASE,
    claim_wake,
    enqueue_wake,
    settle_wake,
    start_wake,
)
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.outcomes import TurnObservation, drain_outcomes
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.prepare import PreparedTurn
from daimon.core.turn.run import RunOutcome
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason, termination_reason
from daimon.testing.db import require_owned_sessionmaker
from mux.contracts.events import Event
from mux.contracts.ids import ChannelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.errors import ScopeViolation
from mux.state.lease import LeaseBusy, Slot
from mux.state.store import StateStore, binding_slot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Evidence = Mapping[str, object]


@dataclass(frozen=True)
class TerminationCase:
    """An independent oracle and its normal host execution, never a verdict.

    Return the actual `RunOutcome` from `run_prepared_turn`, or the actual
    terminal lifecycle `TurnState`. A callback handling a prepared recovery
    exception must retain that lifecycle state instead of inventing a reason
    from the exception. `session=None` means no native session was created.
    Expected fields must be declared before execution, not derived from it.
    """

    name: str
    expected: TerminationReason
    session: ResourceRef | None
    run: Callable[[], Awaitable[TurnState | RunOutcome]]
    expected_error_class: str | None = field(default=None, kw_only=True)
    expected_recovered: bool = field(default=False, kw_only=True)


class TerminationSnapshot(TypedDict):
    case: str
    outcome_id: str
    persisted_rows: int
    expected_reason: str
    actual_reason: str | None
    persisted_reason: str | None
    expected_error_class: str | None
    actual_error_class: str | None
    persisted_error_class: str | None
    expected_session: str | None
    actual_session: str | None
    persisted_session: str | None
    expected_account: str
    persisted_account: str | None
    expected_tenant: str
    persisted_tenant: str | None
    expected_platform: str
    persisted_platform: str | None
    expected_recovered: bool
    actual_recovered: bool
    persisted_recovered: bool | None


class TerminationEvidence(TypedDict):
    required_reasons: tuple[str, ...]
    terminations: tuple[TerminationSnapshot, ...]


def _is_reason(value: object) -> bool:
    return isinstance(value, TerminationReason)


def _host_terminal(value: object) -> TurnState | RunOutcome:
    if not isinstance(value, (TurnState, RunOutcome)):
        raise TypeError("termination callback must return a host terminal state/outcome")
    return value


async def termination_evidence(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    scope: Scope,
    platform: str,
    required_reasons: Sequence[TerminationReason],
    cases: Sequence[TerminationCase],
) -> TerminationEvidence:
    """Read committed host outcomes after normal, scoped offline executions.

    This function never certifies coverage. Providers independently declare
    every reachable reason and install an optional envelope only when ready;
    absent envelopes remain PENDING. A partial reason list or missing execution
    cannot establish complete C18 coverage. Missing rows and unterminated states
    stay missing facts. Expected account/session never fill actual attribution.
    """
    require_owned_sessionmaker(sessionmaker)
    if scope.is_platform or scope.is_legacy_host_authorized:
        raise ScopeViolation(scope.tenant_id, "termination probe requires authorized scope")
    tenant, account = UUID(scope.tenant_id), UUID(scope.account_id)
    # Snapshot the independent oracle before callbacks can mutate caller lists.
    required, scenarios = tuple(required_reasons), tuple(cases)
    if not platform.strip():
        raise ValueError("termination probe requires a platform")
    if (
        not required
        or any(not _is_reason(reason) for reason in required)
        or len(set(required)) != len(required)
    ):
        raise ValueError("declare distinct typed reachable reasons before execution")
    if (
        not scenarios
        or any(not case.name.strip() for case in scenarios)
        or len({case.name for case in scenarios}) != len(scenarios)
        or any(not _is_reason(case.expected) or case.expected not in required for case in scenarios)
    ):
        raise ValueError("declare distinct named cases within the reachable-reason oracle")
    for case in scenarios:
        ref = case.session
        if ref is not None and (
            ref.kind != "session"
            or ref.tenant_id != scope.tenant_id
            or ref.account_id != scope.account_id
        ):
            raise ScopeViolation(ref.id, "termination case has a foreign session")
    snapshots: list[TerminationSnapshot] = []
    for case in scenarios:
        observation = TurnObservation(sessionmaker, tenant, platform)
        actual_recovered = False
        with observation.activate():
            try:
                result = await case.run()
            except Exception as error:
                actual = termination_reason(error)
                actual_error = type(error).__name__
                actual_session = observation.session_id
                observation.finish(error=error)
            else:
                result = _host_terminal(result)
                state = result.state if isinstance(result, RunOutcome) else result
                actual_recovered = result.recovered if isinstance(result, RunOutcome) else False
                actual_session = (
                    result.ma_session_id
                    if isinstance(result, RunOutcome)
                    else observation.session_id
                )
                actual = state.termination or (
                    termination_reason(state.error) if state.error is not None else None
                )
                actual_error = type(state.error).__name__ if state.error is not None else None
                # Prepared execution can already have finished with a more
                # specific recovery reason. Its once-only writer owns the row.
                observation.finish(
                    state=state,
                    reason=actual or TerminationReason.UNKNOWN,
                    recovered=actual_recovered,
                )
        await drain_outcomes()
        async with sessionmaker() as db:
            rows = list(
                await db.scalars(
                    select(TurnOutcome).where(
                        TurnOutcome.id == observation.id, TurnOutcome.tenant_id == tenant
                    )
                )
            )
            row = rows[0] if len(rows) == 1 else None
            snapshots.append(
                TerminationSnapshot(
                    case=case.name,
                    outcome_id=str(observation.id),
                    persisted_rows=len(rows),
                    expected_reason=case.expected.value,
                    actual_reason=actual.value if actual is not None else None,
                    persisted_reason=row.reason if row is not None else None,
                    expected_error_class=case.expected_error_class,
                    actual_error_class=actual_error,
                    persisted_error_class=row.error_class if row is not None else None,
                    expected_session=case.session.id if case.session is not None else None,
                    actual_session=actual_session,
                    persisted_session=row.session_id if row is not None else None,
                    expected_account=str(account),
                    persisted_account=str(row.account_id)
                    if row is not None and row.account_id is not None
                    else None,
                    expected_tenant=str(tenant),
                    persisted_tenant=str(row.tenant_id) if row is not None else None,
                    expected_platform=platform,
                    persisted_platform=row.platform if row is not None else None,
                    expected_recovered=case.expected_recovered,
                    actual_recovered=actual_recovered,
                    persisted_recovered=row.recovered if row is not None else None,
                )
            )
    return TerminationEvidence(
        required_reasons=tuple(reason.value for reason in required), terminations=tuple(snapshots)
    )


def _prepared(turn: PreparedTurn) -> tuple[str | None, int | None, str, str | None]:
    revision = turn.admission.backend_revision
    return (
        revision.profile if revision is not None else None,
        revision.local if revision is not None else None,
        turn.ma_session_id,
        turn.session_ref.provider
        if turn.session_ref is not None
        else (
            "anthropic"
            if revision is None or revision.profile == "anthropic.managed_agents"
            else None
        ),
    )


@dataclass(frozen=True)
class BackendSelectionEnvelope:
    """Callbacks must use normal admit/bind with the channel's current config.

    In particular `existing` must not preselect the old revision itself.
    `without_original` removes only the original driver/runtime registration
    around that same host call and restores it before returning/raising.
    """

    store: StateStore
    scope: Scope
    channel: ChannelRef
    existing_thread_id: str
    new_thread_id: str
    first: Callable[[], Awaitable[PreparedTurn]]
    change_backend: Callable[[], Awaitable[None]]
    existing: Callable[[], Awaitable[PreparedTurn]]
    new_thread: Callable[[], Awaitable[PreparedTurn]]
    without_original: Callable[[], Awaitable[PreparedTurn]]
    mutation_count: Callable[[], int]


async def backend_selection_evidence(envelope: BackendSelectionEnvelope) -> Evidence:
    e = envelope
    if (
        e.scope.is_platform
        or e.scope.is_legacy_host_authorized
        or e.channel.tenant_id != e.scope.tenant_id
        or e.existing_thread_id == e.new_thread_id
    ):
        raise ScopeViolation(e.channel.channel_id, "backend probe requires two authorized threads")

    def prepared(turn: PreparedTurn) -> tuple[str | None, int | None, str, str | None]:
        revision, ref = turn.admission.backend_revision, turn.session_ref
        if (
            str(turn.admission.account_id) != e.scope.account_id
            or str(turn.session_account_id) != e.scope.account_id
            or revision is None
            or revision.channel != e.channel
            or (ref is None and revision.backend != "anthropic")
            or (
                ref is not None
                and (
                    ref.kind != "session"
                    or ref.id != turn.ma_session_id
                    or ref.provider != revision.backend
                    or ref.tenant_id != e.scope.tenant_id
                    or ref.account_id != e.scope.account_id
                )
            )
        ):
            raise ScopeViolation(turn.ma_session_id, "prepared facts have a foreign scope/channel")
        return _prepared(turn)

    async def binding(thread_id: str) -> tuple[str, int, str | None, str] | None:
        row = await e.store.get_binding(
            Slot(
                thread=ThreadRef(channel=e.channel, thread_id=thread_id),
                account_id=e.scope.account_id,
            )
        )
        return (
            (row.profile, row.config_revision, row.native_refs.get("session"), row.provider)
            if row is not None
            else None
        )

    original = await e.store.latest_config_revision(e.channel)
    first = await envelope.first()
    first_facts = prepared(first)
    first_binding = await binding(e.existing_thread_id)
    await envelope.change_backend()
    changed = await e.store.latest_config_revision(e.channel)
    existing = await envelope.existing()
    existing_facts = prepared(existing)
    existing_binding = await binding(e.existing_thread_id)
    new_thread = await envelope.new_thread()
    new_facts = prepared(new_thread)
    new_binding = await binding(e.new_thread_id)
    before = envelope.mutation_count()
    refusal: str | None = None
    try:
        await envelope.without_original()
    except AdmissionDenied as error:
        refusal = error.reason
    return {
        "first": first_facts,
        "existing": existing_facts,
        "new_thread": new_facts,
        "original_config": (original.profile, original.local) if original is not None else None,
        "changed_config": (changed.profile, changed.local) if changed is not None else None,
        "first_binding": first_binding,
        "existing_binding": existing_binding,
        "new_binding": new_binding,
        "missing_original_refusal": refusal,
        "missing_original_mutations": envelope.mutation_count() - before,
    }


@dataclass(frozen=True)
class WakeEnvelope:
    sessionmaker: async_sessionmaker[AsyncSession]
    store: StateStore
    scope: Scope
    request: ContinuationRequest
    replacement: ProviderBinding
    session: ResourceRef
    event: Event
    now: datetime
    run: Callable[[TurnPersistence], Awaitable[None]]


async def wake_evidence(envelope: WakeEnvelope) -> Evidence:
    """Replace a queued wake's binding, reject both stale owners, then run once.

    The provider callback receives an active, current TurnPersistence. It must
    call the normal host driver, which acquires/releases the lease itself, and
    journal the independently expected normalized event through its codec.
    """
    e = envelope
    require_owned_sessionmaker(e.sessionmaker)
    slot = binding_slot(e.replacement)
    request = e.request
    if (
        e.scope.is_platform
        or e.scope.is_legacy_host_authorized
        or str(request.tenant_id) != e.scope.tenant_id
        or str(request.requester_account_id) != e.scope.account_id
        or slot.tenant_id != e.scope.tenant_id
        or slot.account_id not in (None, e.scope.account_id)
        or slot.thread.thread_id != request.thread_id
        or slot.thread.channel.platform != request.platform
        or slot.thread.channel.channel_id != request.parent_channel_id
        or e.event.session_id != e.session.id
        or e.event.native.provider != e.session.provider
    ):
        raise ScopeViolation(e.replacement.id, "wake envelope differs from authorized thread")
    before = await e.store.get_binding(slot)
    if before is None or e.replacement.generation != before.generation + 1:
        raise ValueError("wake probe requires an existing binding and its next generation")
    stale = TurnPersistence(e.store, before, e.scope, operation_key="conformance:stale-wake")
    current = TurnPersistence(
        e.store, e.replacement, e.scope, operation_key=f"wake:{request.idempotency_key}"
    )
    current.check_session(e.scope, e.session)
    if any(event.id == e.event.id for event in await e.store.read_events(e.session.id)):
        raise ValueError("wake probe event must not already exist in the journal")
    await enqueue_wake(e.sessionmaker, request, available_at=e.now)
    old_claim = await claim_wake(e.sessionmaker, idempotency_key=request.idempotency_key, now=e.now)
    if old_claim is None:
        raise ValueError("wake probe did not claim its newly queued request")
    await e.store.put_binding(e.replacement, expected_generation=before.generation)
    takeover_at = e.now + WAKE_CLAIM_LEASE + timedelta(seconds=1)
    claim = await claim_wake(
        e.sessionmaker, idempotency_key=request.idempotency_key, now=takeover_at
    )
    if claim is None:
        raise ValueError("wake probe did not take over the expired unstarted claim")
    stale_start = await start_wake(e.sessionmaker, old_claim, now=takeover_at)
    stale_settle = await settle_wake(e.sessionmaker, old_claim, status="delivered", now=takeover_at)
    stale_calls = 0

    async def stale_pump() -> None:
        nonlocal stale_calls
        stale_calls += 1

    stale_refused = False
    try:
        await stale.run(stale_pump)
    except ScopeViolation:
        stale_refused = True
    started = await start_wake(e.sessionmaker, claim, now=takeover_at)
    runs = 0

    if started:
        runs += 1
        with current.activate():
            await e.run(current)
    settled = await settle_wake(e.sessionmaker, claim, status="delivered", now=takeover_at)
    async with e.sessionmaker() as db:
        row = await get_continuation(db, idempotency_key=request.idempotency_key)
    journal = await e.store.read_events(e.session.id)
    persisted = await e.store.get_binding(slot)
    released = False
    try:
        lease = await e.store.acquire_lease(
            slot,
            holder="conformance:successor",
            turn_id=f"probe:{request.idempotency_key}",
            now=e.now,
            ttl=timedelta(minutes=1),
        )
    except LeaseBusy:
        pass
    else:
        released = True
        await e.store.release_lease(lease)
    return {
        "before_generation": before.generation,
        "current_generation": persisted.generation if persisted is not None else None,
        "replacement_generation": e.replacement.generation,
        "current_session": e.session.id,
        "stale_binding_refused": stale_refused,
        "stale_pump_calls": stale_calls,
        "stale_wake_started": stale_start,
        "stale_wake_settled": stale_settle,
        "wake_started": started,
        "wake_settled": settled,
        "wake_status": row.status if row is not None else None,
        "pump_calls": runs,
        "lease_released": released,
        "journal_event_present": any(
            event.id == e.event.id
            and event.session_id == e.session.id
            and event.type == e.event.type
            and event.payload == e.event.payload
            and event.native.provider == e.event.native.provider
            and event.authority == e.event.authority
            and event.turn_id == e.event.turn_id
            for event in journal
        ),
    }
