"""Fenced state for one admitted mux turn; legacy turns never enter this seam."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from anthropic import APIStatusError
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.receipts import CancelReceipt, OperationStatus
from mux.contracts.resources import ProviderBinding
from mux.drivers.anthropic.transport import single_attempt_mutation
from mux.errors import ProviderError, ScopeViolation
from mux.state.journal import JournalAppend, JournalEntry
from mux.state.lease import Lease, StaleFence
from mux.state.operations import SendClaimed, request_digest
from mux.state.store import StateStore, binding_slot
from pydantic import BaseModel, JsonValue

current_persistence: ContextVar[TurnPersistence | None] = ContextVar(
    "turn_persistence", default=None
)


class UncertainSend(Exception):
    """A prior claim may have reached the provider; replay, never resend it."""


class TurnPersistence:
    """One root operation and its persisted binding's lease.

    A caller resuming an invocation supplies its original operation_key. New
    invocations use new keys. Provider delivery is bounded below the lease TTL;
    lease loss cancels the pump, and every append/claim is fenced by the store.
    """

    def __init__(
        self,
        store: StateStore,
        binding: ProviderBinding,
        scope: Scope,
        *,
        operation_key: str,
        holder: str | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        ttl: timedelta = timedelta(minutes=5),
        renew_interval_s: float = 60.0,
        send_timeout_s: float = 120.0,
    ) -> None:
        slot = binding_slot(binding)
        if (
            scope.is_platform
            or scope.is_legacy_host_authorized
            or slot.tenant_id != scope.tenant_id
            or slot.account_id not in (None, scope.account_id)
            or not operation_key.strip()
        ):
            raise ScopeViolation(binding.id, "turn binding is outside the admitted scope")
        if (
            not 0 < send_timeout_s < ttl.total_seconds()
            or not 0 < renew_interval_s < ttl.total_seconds()
        ):
            raise ValueError("send timeout and renewal interval must be below the lease TTL")
        self.store = store
        self.binding = binding
        self.scope = scope
        self.operation_key = operation_key
        self._holder = holder or secrets.token_hex(16)
        self._now = now
        self._ttl = ttl
        self._renew_interval_s = renew_interval_s
        self._send_timeout_s = send_timeout_s
        self._lease: Lease | None = None

    @contextlib.contextmanager
    def activate(self) -> Iterator[None]:
        token = current_persistence.set(self)
        try:
            yield
        finally:
            current_persistence.reset(token)

    def check_session(self, scope: Scope, session: ResourceRef) -> None:
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
            raise ScopeViolation(self.binding.id, "turn does not hold its binding's lease")
        return self._lease

    async def _check_binding(self) -> None:
        persisted = await self.store.get_binding(binding_slot(self.binding))
        if persisted != self.binding:
            raise ScopeViolation(
                self.binding.id, "turn binding is not the current persisted binding"
            )

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(self._renew_interval_s)
            self._lease = await self.store.renew_lease(
                self._fence(), now=self._now(), ttl=self._ttl
            )

    async def run[T](self, pump: Callable[[], Coroutine[Any, Any, T]]) -> T:
        await self._check_binding()
        self._lease = await self.store.acquire_lease(
            binding_slot(self.binding),
            holder=self._holder,
            turn_id=self.operation_key,
            now=self._now(),
            ttl=self._ttl,
        )
        work: asyncio.Task[T] | None = None
        renew: asyncio.Task[None] | None = None
        try:
            # A binding CAS while acquisition waited must not route a stale turn.
            await self._check_binding()
            work = asyncio.create_task(pump(), name="turn.mux_pump")
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
        session: ResourceRef,
        kind: str,
        request: JsonValue,
        call: Callable[[str], Awaitable[M]],
        response: type[M],
        status: Callable[[M], OperationStatus],
    ) -> M:
        """Intent then send CAS, or restore an acknowledged receipt.

        A definite HTTP refusal permits a fresh attempt. A sent/uncertain
        predecessor never does, including a crash after provider acceptance
        but before the receipt commit. Request bytes are part of the digest.
        """
        self.check_session(self.scope, session)
        digest = request_digest({"session": session.id, "kind": kind, "request": request})
        attempt = 0
        while True:
            key = f"{self.operation_key}:{kind}:{attempt}"
            begun = await self.store.begin_operation(
                self.scope,
                key=key,
                request_digest=digest,
                operation_id=key,
                slot=binding_slot(self.binding),
                now=self._now(),
            )
            record = begun.record
            if record.operation.status == "failed":
                attempt += 1
                continue
            if record.operation.status in ("accepted", "processed"):
                cached = response.model_validate(dict(record.result))
                if isinstance(cached, CancelReceipt):
                    # M0 composes a fresh backend namespace around the same
                    # authorized client on restart. Preserve the native session
                    # and scope, then address it in this backend's namespace.
                    self.check_session(self.scope, cached.session)
                    return cast(M, cached.model_copy(update={"session": session}))
                return cached
            if record.operation.status != "pending":
                raise UncertainSend(f"{kind} was already claimed; reconcile before delivery")
            try:
                await self.store.claim_send(self.scope, key, now=self._now(), fence=self._fence())
            except SendClaimed as error:
                raise UncertainSend(f"{kind} was claimed by another sender") from error
            break
        try:
            # Refresh under the database clock before starting bounded provider I/O.
            self._lease = await self.store.renew_lease(
                self._fence(), now=self._now(), ttl=self._ttl
            )
            async with asyncio.timeout(self._send_timeout_s):
                with single_attempt_mutation():
                    receipt = await call(key)
        except BaseException as error:
            # Only an explicit client refusal proves this request was rejected.
            cause = error.__cause__ if isinstance(error, ProviderError) else error
            refused = isinstance(cause, APIStatusError) and 400 <= cause.status_code < 500
            with contextlib.suppress(StaleFence):
                await self.store.advance_operation(
                    self.scope,
                    key,
                    "failed" if refused else "outcome_unknown",
                    now=self._now(),
                    fence=self._fence(),
                    resource=session,
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
