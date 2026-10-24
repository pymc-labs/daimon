"""Fenced state for one admitted mux turn; legacy turns never enter this seam."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Sequence
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import structlog
from daimon.core.stores.mux_state import BeforeClaim, PostgresStateStore
from daimon.core.turn.mutation_gate import recheck_retry
from mux.contracts.events import AgentMessagePayload, Event, NativeProvenance
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.receipts import CancelReceipt, OperationStatus
from mux.contracts.resources import ProviderBinding
from mux.drivers.anthropic import classify_mutation_failure, single_attempt_mutation
from mux.errors import ProviderError, ScopeViolation
from mux.state.journal import JournalAppend, JournalEntry, is_authoritative
from mux.state.lease import Lease, LeaseBusy, Slot, StaleFence
from mux.state.mutation_retry import MutationFailure, MutationRetryPolicy, RetryStop
from mux.state.operations import OperationRecord, SendClaimed, request_digest
from mux.state.store import StateStore, binding_slot
from pydantic import BaseModel, JsonValue

if TYPE_CHECKING:
    from daimon.core.fleet import FleetRun, FleetWorker, ResultPointer


log = structlog.get_logger(__name__)

# Kinds that never pass the root budget check, and so never count an input.
_UNCOUNTED_KINDS = frozenset({"cancel", "archive", "recovery-interrupt"})

# Stop requests exist because the turn was cancelled: the cancel signal never
# holds back their retries (the deadline and every other rule still apply).
_STOP_KINDS = frozenset({"cancel", "recovery-stop", "recovery-interrupt"})

_NOT_SENT_CANCELLED = MutationFailure("rejected", retryable=False, reason="cancelled_before_send")


class _CancelledBeforeSend(Exception):
    """A claimed retry found the turn cancelled before its provider call."""


current_persistence: ContextVar[TurnPersistence | None] = ContextVar(
    "turn_persistence", default=None
)


class UncertainSend(Exception):
    """A prior claim may have reached the provider; replay, never resend it."""


class LeaseWaitCancelled(Exception):
    """The turn's cancel signal fired while it waited for the thread's lease."""


class BindingSuperseded(ScopeViolation):
    """The binding changed while the turn queued for its lease.

    Raised only for a turn that waits: its holder may have replaced the session
    it was prepared against, and the caller adopts that replacement.
    """


@dataclass(frozen=True)
class LeaseWait:
    """Wait for a busy thread lease instead of refusing the turn.

    `timeout_s` is the patience of this one wait, measured on the event loop's
    monotonic clock; the caller derives it from the turn's own ceiling, so a
    waiting turn never outlives the bound a running one has. Whether the lease
    is free is always the store's call (the Postgres store compares against the
    database clock), so a holder that dies frees the slot when its TTL lapses.
    `cancel` is the turn's cancel signal, honoured while waiting.
    """

    timeout_s: float
    cancel: asyncio.Event | None = None
    poll_initial_s: float = 0.05
    poll_max_s: float = 1.0


@dataclass(frozen=True)
class MutationRetry:
    """Host limits for retrying a definitely refused mutation within one call.

    `deadline` is on the `time.monotonic()` clock (the event loop's default)
    and bounds every wait, so no retry outlives the turn. `cancel` is the
    turn's cancel signal: it ends a wait at once and nothing more is sent.
    Without an explicit value, `TurnPersistence` takes both from its
    `LeaseWait`, which the host derives from the turn's ceiling.
    """

    policy: MutationRetryPolicy = field(default_factory=MutationRetryPolicy)
    deadline: float | None = None
    cancel: asyncio.Event | None = None


class TurnPersistence:
    """One root operation and its persisted binding's lease.

    A caller resuming an invocation supplies its original operation_key. New
    invocations use new keys. Provider delivery is bounded below the lease TTL;
    lease loss cancels the pump, and every append/claim is fenced by the store.
    """

    def __init__(
        self,
        store: StateStore,
        binding: ProviderBinding | None,
        scope: Scope,
        *,
        operation_key: str,
        slot: Slot | None = None,
        holder: str | None = None,
        source_agent_id: str | None = None,
        session_ref: ResourceRef | None = None,
        fleet_capture: bool = False,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        ttl: timedelta = timedelta(minutes=5),
        renew_interval_s: float = 60.0,
        send_timeout_s: float = 120.0,
        lease_wait: LeaseWait | None = None,
        retry: MutationRetry | None = None,
        execution_context: Callable[[Lease], AbstractAsyncContextManager[None]] | None = None,
    ) -> None:
        slot = binding_slot(binding) if binding is not None else slot
        if slot is None:
            raise ScopeViolation(operation_key, "preparation requires an explicit private slot")
        if (
            scope.is_platform
            or scope.is_legacy_host_authorized
            or slot.tenant_id != scope.tenant_id
            or slot.account_id not in (None, scope.account_id)
            or not operation_key.strip()
        ):
            raise ScopeViolation(operation_key, "turn binding is outside the admitted scope")
        if (
            not 0 < send_timeout_s < ttl.total_seconds()
            or not 0 < renew_interval_s < ttl.total_seconds()
        ):
            raise ValueError("send timeout and renewal interval must be below the lease TTL")
        self.root_claim: Callable[[ResourceRef | None, bool], BeforeClaim] | None = None
        self.store = store
        self._binding = binding
        self._slot = slot
        self.scope = scope
        self.operation_key = operation_key
        self.source_agent_id = source_agent_id
        self.session_ref = session_ref
        self.fleet_capture = fleet_capture
        self._holder = holder or secrets.token_hex(16)
        self._now = now
        self._ttl = ttl
        self._renew_interval_s = renew_interval_s
        self._send_timeout_s = send_timeout_s
        self._lease: Lease | None = None
        self._lease_wait = lease_wait
        self._retry = retry or MutationRetry(
            deadline=time.monotonic() + lease_wait.timeout_s if lease_wait is not None else None,
            cancel=lease_wait.cancel if lease_wait is not None else None,
        )
        self._execution_context = execution_context
        self.lease_acquired = False

    @property
    def slot(self) -> Slot:
        return self._slot

    @property
    def binding(self) -> ProviderBinding:
        if self._binding is None:
            raise ScopeViolation(self.operation_key, "preparation has no acknowledged binding")
        return self._binding

    @contextlib.contextmanager
    def activate(self) -> Iterator[None]:
        token = current_persistence.set(self)
        try:
            yield
        finally:
            current_persistence.reset(token)

    def check_session(self, scope: Scope, session: ResourceRef | None) -> None:
        if session is None:
            if scope != self.scope or self._binding is not None:
                raise ScopeViolation(self.operation_key, "turn context does not own preparation")
            self._fence()
            return
        if (
            scope != self.scope
            or session.kind != "session"
            or session.provider != self.binding.provider
            or session.id != self.binding.native_refs.get("session")
            or session.tenant_id != scope.tenant_id
            or session.account_id != scope.account_id
        ):
            raise ScopeViolation(session.id, "turn context does not own this session")

    def _fence(self) -> Lease:
        if self._lease is None:
            raise ScopeViolation(self.operation_key, "turn does not hold its binding's lease")
        return self._lease

    async def _check_binding(self, *, queued: bool = False) -> None:
        persisted = await self.store.get_binding(self._slot)
        if persisted != self._binding:
            if queued:
                raise BindingSuperseded(
                    self.operation_key, "turn binding changed while the turn waited for its lease"
                )
            raise ScopeViolation(
                self.operation_key, "turn binding is not the current persisted binding"
            )

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(self._renew_interval_s)
            self._lease = await self.store.renew_lease(
                self._fence(), now=self._now(), ttl=self._ttl
            )

    async def _acquire_lease(self) -> Lease:
        """Take the binding's lease; with `lease_wait`, queue behind a live holder.

        Each poll is one store acquisition, so a lease the holder released or
        let expire is taken by whichever waiter polls next. Nothing is held
        while waiting: a cancelled or timed-out waiter has nothing to release.
        """
        wait = self._lease_wait
        loop = asyncio.get_running_loop()
        give_up = loop.time() + wait.timeout_s if wait is not None else 0.0
        delay = wait.poll_initial_s if wait is not None else 0.0
        while True:
            if wait is not None:
                if wait.cancel is not None and wait.cancel.is_set():
                    raise LeaseWaitCancelled(self.operation_key)
                if loop.time() >= give_up:
                    raise TimeoutError(f"{self._slot.describe()} stayed leased by another turn")
            try:
                acquired = await self.store.acquire_lease(
                    self._slot,
                    holder=self._holder,
                    turn_id=self.operation_key,
                    now=self._now(),
                    ttl=self._ttl,
                )
                if wait is not None and loop.time() >= give_up:
                    with contextlib.suppress(StaleFence):
                        await self.store.release_lease(acquired)
                    raise TimeoutError(f"{self._slot.describe()} exceeded its lease wait")
                return acquired
            except LeaseBusy:
                if wait is None:
                    raise
            if wait.cancel is not None and wait.cancel.is_set():
                raise LeaseWaitCancelled(self.operation_key)
            patience = give_up - loop.time()
            if patience <= 0:
                raise TimeoutError(f"{self._slot.describe()} stayed leased by another turn")
            await self._sleep_or_cancel(min(delay, patience), wait.cancel)
            delay = min(delay * 2, wait.poll_max_s)

    @staticmethod
    async def _sleep_or_cancel(delay: float, cancel: asyncio.Event | None) -> None:
        if cancel is None:
            await asyncio.sleep(delay)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(cancel.wait(), timeout=delay)

    async def run[T](self, pump: Callable[[], Coroutine[Any, Any, T]]) -> T:
        async def execute() -> T:
            # Origin authority belongs to execution, never to lease waiters.
            # Its cleanup runs before the acquired lease is released, including
            # cancellation, provider failure and lease-renewal failure.
            context = (
                self._execution_context(self._fence())
                if self._execution_context is not None
                else contextlib.nullcontext()
            )
            async with context:
                return await pump()

        await self._check_binding()
        self._lease = await self._acquire_lease()
        work: asyncio.Task[T] | None = None
        renew: asyncio.Task[None] | None = None
        try:
            # A binding CAS while acquisition waited must not route a stale turn.
            await self._check_binding(queued=self._lease_wait is not None)
            if (
                self._lease_wait is not None
                and self._lease_wait.cancel is not None
                and self._lease_wait.cancel.is_set()
            ):
                raise LeaseWaitCancelled(self.operation_key)
            self.lease_acquired = True
            work = asyncio.create_task(execute(), name="turn.mux_pump")
            renew = asyncio.create_task(self._renew(), name="turn.mux_lease")
            done, _ = await asyncio.wait((work, renew), return_when=asyncio.FIRST_COMPLETED)
            if renew in done:
                await renew  # A renewal failure owns the exit, even if the pump also finished.
            return await work
        finally:
            tasks = [task for task in (work, renew) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                *(cast(Awaitable[object], task) for task in tasks), return_exceptions=True
            )
            with contextlib.suppress(StaleFence):
                await self.store.release_lease(self._fence())
            self._lease = None

    async def mutate[M: BaseModel](
        self,
        session: ResourceRef | None,
        kind: str,
        request: JsonValue,
        call: Callable[[str], Awaitable[M]],
        response: type[M],
        status: Callable[[M], OperationStatus],
        *,
        guard: Callable[[], Awaitable[None]] | None = None,
        retry_failed: bool = True,
        operation_key: str | None = None,
        begin: Callable[[str, str], Awaitable[OperationRecord]] | None = None,
    ) -> M:
        """Intent then send CAS, or restore an acknowledged receipt.

        Attempt `n` of a mutation is the durable operation `<key>:<kind>:<n>`.
        A sent/uncertain attempt is never followed by another, including a
        crash after provider acceptance but before the receipt commit. Request
        bytes are part of the digest.

        An attempt may follow a retryable refusal only when `MutationRetryPolicy`
        allows it given the attempt count, attempt 0's start, the refusal's
        recorded time plus Retry-After and the host deadline. The same durable
        rules apply whether this call saw the refusal or a later invocation
        (restart, replay) finds it, so neither can reset or shorten the
        allowance. A non-retryable refusal (a 400) is never retried here, but a
        later explicit invocation may take a fresh attempt within the attempt
        cap, as before: it proved nothing ran. A refusal recorded without a
        classification (written before classification existed) is never
        followed. Each retry is claimed under the current lease fence and,
        except for stop requests, rechecks the turn's cancel signal right
        before the provider call. This call retries a refusal itself only for
        its own default keys: allocations (`retry_failed=False`), a
        caller-supplied `begin` and root-counted inputs never retry in-call.
        """
        if session is not None:
            self.check_session(self.scope, session)
        digest = request_digest(
            {
                "session": session.id if session is not None else None,
                "kind": kind,
                "request": request,
            }
        )
        may_retry = retry_failed and begin is None and not self._counts_input(kind)
        base = f"{operation_key or self.operation_key}:{kind}"
        # The in-call failure a retry follows; re-raised if the retry stops.
        refused: Exception | None = None
        while True:
            attempt = 0
            first: OperationRecord | None = None
            previous: OperationRecord | None = None
            while True:
                key = f"{base}:{attempt}"
                if begin is None:
                    begun = await self.store.begin_operation(
                        self.scope,
                        key=key,
                        request_digest=digest,
                        operation_id=key,
                        slot=self._slot,
                        now=self._now(),
                    )
                    record = begun.record
                else:
                    record = await begin(key, digest)
                first = first or record
                if record.operation.status != "failed":
                    break
                if not retry_failed:
                    raise ProviderError(
                        "upstream", retryable=False, native_code="prior_allocation_refused"
                    )
                previous = record
                attempt += 1
            if record.operation.status in ("accepted", "processed"):
                cached = response.model_validate_json(json.dumps(dict(record.result)))
                if isinstance(cached, CancelReceipt) and session is not None:
                    # M0 composes a fresh backend namespace around the same
                    # authorized client on restart. Preserve the native session
                    # and scope, then address it in this backend's namespace.
                    self.check_session(self.scope, cached.session)
                    return cast(M, cached.model_copy(update={"session": session}))
                return cached
            if record.operation.status != "pending":
                raise UncertainSend(f"{kind} was already claimed; reconcile before delivery")
            gated = previous is not None and kind not in _STOP_KINDS
            if previous is not None:
                await self._await_retry(kind, attempt, first, previous, refused)
            if gated:
                await recheck_retry()
            if guard is not None:
                await guard()
            if gated and self._cancelled():
                raise self._retry_stopped(kind, attempt, previous, "cancelled", refused)
            await self._claim(session, kind, key, request)
            try:
                return await self._send(session, kind, key, call, status, gated=gated)
            except _CancelledBeforeSend:
                pass  # Recorded as refused and unsent; raised below, outside this handler.
            except Exception as error:
                if not may_retry:
                    raise
                failure = classify_mutation_failure(error)
                if not failure.retryable:
                    error.add_note(
                        f"{kind}: attempt {attempt + 1} of {self._retry.policy.max_attempts} "
                        f"{failure.delivery} ({failure.reason}); not retried: not_retryable"
                    )
                    raise
                # Walk the durable records again: the recorded refusal decides.
                refused = error
                continue
            raise self._retry_stopped(kind, attempt, previous, "cancelled", refused)

    async def _await_retry(
        self,
        kind: str,
        attempt: int,
        first: OperationRecord,
        previous: OperationRecord,
        refused: Exception | None,
    ) -> None:
        """Wait until attempt `attempt` may follow the refusal `previous`, or raise."""
        policy = self._retry.policy
        failure = MutationFailure.from_json(previous.result.get("failure"))
        if failure is None:
            raise self._retry_stopped(kind, attempt, previous, "unclassified_refusal", refused)
        if not failure.retryable and failure.delivery == "rejected":
            # Never retried in-call. A later explicit invocation (main's
            # confirmation recovery resends after a 400) may take one fresh
            # attempt: the refusal proved nothing ran, so it cannot duplicate.
            if attempt >= policy.max_attempts:
                raise self._retry_stopped(kind, attempt, previous, "attempts_exhausted", refused)
            return
        decision = policy.decide(
            failure,
            attempts_made=attempt,
            first_began=first.operation.created_at,
            refused_at=previous.operation.updated_at,
            now=self._now(),
            remaining_s=self._retry_remaining_s(),
        )
        log.info(
            "mux.mutation_retry",
            operation=previous.operation.key,
            kind=kind,
            attempt=attempt,
            max_attempts=policy.max_attempts,
            delivery=failure.delivery,
            reason=failure.reason,
            retry_after_s=failure.retry_after_s,
            wait_s=decision.wait_s if decision.retry else None,
            stop=decision.stop,
        )
        if decision.stop is not None:
            raise self._retry_stopped(kind, attempt, previous, decision.stop, refused)
        honour_cancel = kind not in _STOP_KINDS
        if await self._wait_to_retry(decision.wait_s, honour_cancel=honour_cancel):
            raise self._retry_stopped(kind, attempt, previous, "cancelled", refused)

    def _retry_stopped(
        self,
        kind: str,
        attempt: int,
        previous: OperationRecord | None,
        stop: RetryStop,
        refused: Exception | None,
    ) -> Exception:
        """The error for a retry that will not be sent, with its attempt count.

        In-call, the refusal itself is re-raised so hosts keep mapping it (a
        429 stays a rate limit); a later invocation gets a `ProviderError`.
        """
        failure = (
            MutationFailure.from_json(previous.result.get("failure"))
            if previous is not None
            else None
        )
        seen = f"{failure.delivery} ({failure.reason})" if failure is not None else "unclassified"
        if stop == "cancelled":
            note = f"{kind}: attempt {attempt} {seen}; retry abandoned: cancelled"
        else:
            note = (
                f"{kind}: attempt {attempt} of {self._retry.policy.max_attempts} {seen}; "
                f"not retried: {stop}"
            )
        if refused is not None:
            refused.add_note(note)
            return refused
        error = ProviderError(
            "rate_limited" if failure is not None and failure.reason == "http_429" else "upstream",
            retryable=False,
            native_code=f"mutation_{stop}",
        )
        error.add_note(f"{note}; nothing was sent")
        return error

    def _counts_input(self, kind: str) -> bool:
        """A root-funded claim counts each attempt key; a retry would count twice."""
        return self.root_claim is not None and kind not in _UNCOUNTED_KINDS

    def _retry_remaining_s(self) -> float | None:
        deadline = self._retry.deadline
        return None if deadline is None else deadline - time.monotonic()

    def _cancelled(self) -> bool:
        cancel = self._retry.cancel
        return cancel is not None and cancel.is_set()

    async def _wait_to_retry(self, delay: float, *, honour_cancel: bool) -> bool:
        """Sleep before a retry; True when the turn was cancelled meanwhile."""
        cancel = self._retry.cancel if honour_cancel else None
        if delay > 0 and (cancel is None or not cancel.is_set()):
            await self._sleep_or_cancel(delay, cancel)
        return cancel is not None and cancel.is_set()

    async def _claim(
        self, session: ResourceRef | None, kind: str, key: str, request: JsonValue
    ) -> None:
        """Win `pending -> sent` under the lease fence (and the root budget)."""
        try:
            store = self.store
            check = self.root_claim
            context = contextlib.nullcontext()
            if check is not None and kind not in _UNCOUNTED_KINDS:
                if not isinstance(store, PostgresStateStore):
                    raise ScopeViolation("root-budget", "transactional root store required")
                inputs = request.get("events") if isinstance(request, dict) else request
                if kind == "send" and (not isinstance(inputs, list) or len(inputs) != 1):
                    raise ScopeViolation("root-budget", "one counted input required")
                context = store.checking_claims(check(session, kind == "send"), compose=True)
            with context:
                await store.claim_send(self.scope, key, now=self._now(), fence=self._fence())
        except SendClaimed as error:
            raise UncertainSend(f"{kind} was claimed by another sender") from error

    async def _send[M: BaseModel](
        self,
        session: ResourceRef | None,
        kind: str,
        key: str,
        call: Callable[[str], Awaitable[M]],
        status: Callable[[M], OperationStatus],
        *,
        gated: bool,
    ) -> M:
        """One provider attempt for a claimed key; record what it proves.

        A `gated` attempt (a retry) checks the cancel signal after the lease
        renewal, the last await before the provider call: a turn cancelled
        while the retry was being claimed records it as refused, unsent.
        """
        try:
            # Refresh under the database clock before starting bounded provider I/O.
            self._lease = await self.store.renew_lease(
                self._fence(), now=self._now(), ttl=self._ttl
            )
            if gated and self._cancelled():
                raise _CancelledBeforeSend
            async with asyncio.timeout(self._send_timeout_s):
                with single_attempt_mutation():
                    receipt = await call(key)
        except BaseException as error:
            # Only a driver-classified refusal proves this request was not
            # executed; everything else, including cancellation, stays unknown.
            failure = (
                _NOT_SENT_CANCELLED
                if isinstance(error, _CancelledBeforeSend)
                else classify_mutation_failure(error)
            )
            refused = failure.delivery == "rejected"
            with contextlib.suppress(StaleFence):
                await self.store.advance_operation(
                    self.scope,
                    key,
                    "failed" if refused else "outcome_unknown",
                    now=self._now(),
                    fence=self._fence(),
                    resource=session,
                    result={"failure": failure.as_json()} if refused else None,
                )
            if isinstance(error, TimeoutError):
                raise UncertainSend(f"{kind} delivery timed out") from error
            raise
        await self.store.advance_operation(
            self.scope,
            key,
            status(receipt),
            now=self._now(),
            fence=self._fence(),
            resource=session,
            result=receipt.model_dump(mode="json"),
        )
        return receipt

    async def record(
        self, session: ResourceRef, event: Event, *, source_key: str | None = None
    ) -> None:
        self.check_session(self.scope, session)
        source = source_key if source_key is not None else event.native.event_id or event.id
        raw_revision = event.payload.get("revision", 1)
        revision = raw_revision if isinstance(raw_revision, int) else 1
        await self.store.append_events(
            session,
            (JournalEntry(source_key=source, revision=revision, event=event),),
            fence=self._fence(),
            cursor=event.native.cursor or source,
            now=self._now(),
        )

    async def record_many(
        self,
        session: ResourceRef,
        events: Sequence[Event],
        *,
        cursor: str,
        source_key: Callable[[Event], str] | None = None,
    ) -> JournalAppend:
        """Publish a collected snapshot atomically under the active host lease.

        The caller supplies its completed-snapshot cursor, including for an
        empty snapshot. Source identities and revisions match record(); all
        entries, projection and cursor commit in one StateStore transaction.
        A caller may select a derived neutral source identity without altering
        the event or its provider provenance; omission preserves native identity.
        """
        self.check_session(self.scope, session)
        entries: list[JournalEntry] = []
        for event in events:
            source = (
                source_key(event) if source_key is not None else event.native.event_id or event.id
            )
            raw_revision = event.payload.get("revision", 1)
            revision = raw_revision if isinstance(raw_revision, int) else 1
            entries.append(JournalEntry(source_key=source, revision=revision, event=event))
        return await self.store.append_events(
            session, entries, fence=self._fence(), cursor=cursor, now=self._now()
        )

    async def stopped(self, key: str, session: ResourceRef) -> None:
        """Independent stop evidence completes cancel, never the cancel ack alone."""
        self.check_session(self.scope, session)
        await self.store.advance_operation(
            self.scope,
            key,
            "processed",
            now=self._now(),
            fence=self._fence(),
            resource=session,
        )

    async def publish_worker_result(
        self,
        run: FleetRun,
        worker: FleetWorker,
        session: ResourceRef,
        *,
        accepted_event_ids: frozenset[str],
    ) -> ResultPointer:
        """Point at an accepted recorded message under this worker's active lease."""
        from daimon.core.fleet import FleetConflict, append_result

        self.check_session(self.scope, session)
        lease = self._fence()
        after = -1
        result: Event | None = None
        while events := await self.store.read_events(session.id, after=after):
            for event in events:
                if (
                    event.type == "agent.message"
                    and is_authoritative(event)
                    and (
                        event.id in accepted_event_ids
                        or event.native.event_id in accepted_event_ids
                    )
                    and AgentMessagePayload.model_validate(event.payload).complete
                ):
                    result = event
            after = events[-1].sequence
        if result is None:
            raise FleetConflict("worker has no accepted recorded final message")
        projection = await self.store.projection(session.id)
        if projection is None:
            raise FleetConflict("worker has no owned journal projection")
        return await append_result(
            self.store,
            run,
            worker,
            session,
            result,
            lease=lease,
            cursor=projection.cursor,
            now=self._now(),
        )

    async def gap(self, session: ResourceRef) -> None:
        """Checkpoint a stream ending without inventing a terminal outcome."""
        self.check_session(self.scope, session)
        projection = await self.store.projection(session.id)
        cursor = projection.cursor if projection is not None else ""
        source = f"{self.operation_key}:gap:{cursor}"
        event = Event(
            id=source,
            session_id=session.id,
            sequence=0,
            type="session.history_gap",
            observed_at=self._now(),
            authority="gap",
            payload={"domain": "turn_stream", "after": cursor or None, "recoverable": True},
            native=NativeProvenance(provider=session.provider, api_revision="host-turn"),
        )
        await self.store.append_events(
            session,
            (JournalEntry(source_key=source, event=event),),
            fence=self._fence(),
            cursor=cursor,
            now=self._now(),
        )
