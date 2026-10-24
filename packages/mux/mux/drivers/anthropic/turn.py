"""Unwired Events port over an injected SDK client. No journal or host policy."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

import httpx
from anthropic import (
    NOT_GIVEN,
    APIConnectionError,
    AsyncAnthropic,
    AsyncStream,
    omit,
)
from anthropic.types.beta.sessions import BetaManagedAgentsStreamSessionEvents

from mux.contracts.actions import InputEvent
from mux.contracts.events import Event
from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope
from mux.contracts.receipts import CancelReceipt, SendReceipt, StopObservation
from mux.contracts.resources import ProjectionSnapshot
from mux.drivers.anthropic.actions import translate_inputs
from mux.drivers.anthropic.cancel import observed_stop
from mux.drivers.anthropic.normalize import EventNormalizer, object_json
from mux.drivers.anthropic.reconcile import reconcile_session
from mux.drivers.anthropic.resources._authorization import (
    ResourceAuthorization,
    authorize,
    check_ref,
)
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter
from mux.drivers.anthropic.transport import mutation_client, never_written
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability


class EventHistoryWalk(Protocol):
    """Anthropic's full chronological event walk, as owned records."""

    def walk(self, scope: Scope, session: ResourceRef) -> AsyncIterator[Event]: ...


class AnthropicEventHistoryWalk:
    """One normalizer across the SDK's paginator, with its original kwargs."""

    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
        authorization: ResourceAuthorization | None = None,
    ) -> None:
        self._client = client
        self._account_scope_id = account_scope_id
        self._authorization = authorization

    async def walk(self, scope: Scope, session: ResourceRef) -> AsyncIterator[Event]:
        check_ref(scope, session, self._account_scope_id, "session")
        authorize(self._authorization, scope, "session", session.id)
        normalizer = EventNormalizer(session)
        try:
            # Every reconnect walks the whole session log; the SDK default page size
            # would multiply the round trips on a long session fivefold.
            async for native in provider_iter(
                self._client.beta.sessions.events.list(session_id=session.id, limit=100)
            ):
                yield normalizer.normalize(
                    object_json(native.model_dump(mode="json")), observed_at=datetime.now(UTC)
                )
        except httpx.HTTPError as error:
            raise ProviderError("transient_network", retryable=True) from error
        except (ValueError, KeyError, TypeError) as error:
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_event"
            ) from error


class _NormalizedStream(AsyncIterator[Event]):
    """A closable stream, including the open-but-not-yet-iterated case."""

    def __init__(
        self,
        stream: AsyncStream[BetaManagedAgentsStreamSessionEvents],
        session: ResourceRef,
        previews: bool,
    ) -> None:
        self._stream = stream
        self._source = provider_iter(stream).__aiter__()
        self._normalizer = EventNormalizer(session)
        self._previews = previews
        self._closed = False

    def __aiter__(self) -> _NormalizedStream:
        return self

    async def __anext__(self) -> Event:
        if self._closed:
            raise StopAsyncIteration
        try:
            while True:
                raw = await self._source.__anext__()
                event = self._normalizer.normalize(
                    object_json(raw.model_dump(mode="json")), observed_at=datetime.now(UTC)
                )
                if self._previews or event.authority != "preview":
                    return event
        except httpx.HTTPError as error:
            await self.aclose()
            raise ProviderError("transient_network", retryable=True) from error
        except (ValueError, KeyError, TypeError) as error:
            await self.aclose()
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_event"
            ) from error
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._stream.close()


class AnthropicEvents:
    """Authorize before I/O; return only owned values.

    The host persists operation intent and owns idempotency/leases. Anthropic
    offers no turn precondition or resumable SSE cursor; these requests fail
    before I/O rather than silently weakening those guarantees. A lost POST
    acknowledgement reports outcome_unknown and is never retried by this port
    or by its SDK client; turn mutations always disable automatic retries.
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
        authorization: ResourceAuthorization | None = None,
        *,
        stream_read_timeout_s: float | None = None,
        on_stop_event: Callable[[Event], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._account_scope_id = account_scope_id
        self._authorization = authorization
        self._stream_read_timeout_s = stream_read_timeout_s
        self._on_stop_event = on_stop_event

    def _check(self, scope: Scope, session: ResourceRef) -> None:
        check_ref(scope, session, self._account_scope_id, "session")
        authorize(self._authorization, scope, "session", session.id)

    async def send(
        self,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None = None,
    ) -> SendReceipt:
        self._check(scope, session)
        if expected_turn is not None:
            raise UnsupportedCapability(("expected_turn",), "anthropic.managed_agents")
        inputs = translate_inputs(events)
        try:
            result = await provider_call(
                mutation_client(self._client, single_attempt=True).beta.sessions.events.send(
                    session.id, events=inputs
                )
            )
        except ProviderError as error:
            cause = error.__cause__
            # A request that never left this process is a refusal, not a lost
            # acknowledgement: it raises, so the host may take a fresh attempt.
            if isinstance(cause, APIConnectionError) and never_written(cause) is None:
                return SendReceipt(operation_id=key, status="outcome_unknown", input_ids=())
            raise
        return SendReceipt(
            operation_id=key,
            status="processed" if result.data is not None else "queued",
            input_ids=tuple(event.id for event in result.data or ()),
        )

    async def open_stream(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        after: str | None = None,
        previews: bool = False,
    ) -> AsyncIterator[Event]:
        self._check(scope, session)
        if after is not None:
            raise UnsupportedCapability(("stream_cursor",), "anthropic.managed_agents")
        stream = await provider_call(
            self._client.beta.sessions.events.stream(
                session_id=session.id,
                event_deltas=["agent.message"] if previews else omit,
                timeout=httpx.Timeout(self._stream_read_timeout_s, connect=5.0)
                if self._stream_read_timeout_s is not None
                else NOT_GIVEN,
            )
        )
        return _NormalizedStream(stream, session, previews)

    async def stream(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        after: str | None = None,
        previews: bool = False,
    ) -> AsyncIterator[Event]:
        stream = await self.open_stream(scope, session, after=after, previews=previews)
        try:
            async for event in stream:
                yield event
        finally:
            # open_stream returns our closable iterator, not a raw SDK stream.
            assert isinstance(stream, _NormalizedStream)
            await stream.aclose()

    async def list(self, scope: Scope, session: ResourceRef, *, page: PageRequest) -> Page[Event]:
        self._check(scope, session)
        result = await provider_call(
            self._client.beta.sessions.events.list(
                session.id,
                page=page.cursor if page.cursor is not None else omit,
                limit=page.limit if page.limit is not None else omit,
                order=page.order if page.order is not None else omit,
            )
        )
        normalizer = EventNormalizer(session)
        try:
            data = tuple(
                normalizer.normalize(
                    object_json(event.model_dump(mode="json")), observed_at=datetime.now(UTC)
                )
                for event in result.data
            )
        except (ValueError, KeyError, TypeError) as error:
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_event"
            ) from error
        has_more = result.has_next_page()
        return Page(
            data=data,
            has_more=has_more,
            next_cursor=result.next_page if has_more else None,
        )

    async def reconcile(self, scope: Scope, session: ResourceRef) -> ProjectionSnapshot:
        self._check(scope, session)
        return await reconcile_session(self._client, session)

    async def cancel(
        self, scope: Scope, session: ResourceRef, *, turn_id: str, key: str
    ) -> CancelReceipt:
        self._check(scope, session)
        requested_at = datetime.now(UTC)
        try:
            await provider_call(
                mutation_client(self._client, single_attempt=True).beta.sessions.events.send(
                    session.id, events=[{"type": "user.interrupt"}]
                )
            )
        except ProviderError as error:
            cause = error.__cause__
            # A request that never left this process is a refusal, not a lost
            # acknowledgement: it raises, so the host may take a fresh attempt.
            if isinstance(cause, APIConnectionError) and never_written(cause) is None:
                return CancelReceipt(
                    operation_id=key,
                    session=session,
                    turn_id=turn_id,
                    status="outcome_unknown",
                    requested_at=requested_at,
                )
            raise
        return CancelReceipt(
            operation_id=key,
            session=session,
            turn_id=turn_id,
            status="requested",
            requested_at=requested_at,
        )

    async def wait_stopped_before_cancel(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        cancel: Callable[[], Awaitable[CancelReceipt]],
        deadline: datetime,
    ) -> tuple[CancelReceipt, StopObservation]:
        """Main's instant-ack fix, with the host retaining mutation-claim ownership."""
        self._check(scope, session)
        receipt: CancelReceipt | None = None
        stream: _NormalizedStream | None = None
        try:
            async with asyncio.timeout(max(0.0, (deadline - datetime.now(UTC)).total_seconds())):
                native = await provider_call(
                    self._client.beta.sessions.events.stream(session_id=session.id)
                )
                stream = _NormalizedStream(native, session, False)
                receipt = await cancel()
                if receipt.session != session:
                    raise ScopeViolation(
                        receipt.session.id, "cancel receipt belongs to another session"
                    )
                async for event in stream:
                    if self._on_stop_event is not None:
                        await self._on_stop_event(event)
                    if (stop := observed_stop(event, receipt)) is not None:
                        return receipt, stop
        except TimeoutError:
            if receipt is None:
                raise
        finally:
            if stream is not None:
                await stream.aclose()
        return receipt, StopObservation(
            receipt_operation_id=receipt.operation_id,
            stopped=False,
            outcome=None,
            observed_at=datetime.now(UTC),
        )

    async def wait_stopped(
        self, scope: Scope, receipt: CancelReceipt, *, deadline: datetime
    ) -> StopObservation:
        self._check(scope, receipt.session)
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        stream: AsyncIterator[Event] | None = None
        try:
            if remaining > 0:
                async with asyncio.timeout(remaining):
                    # Preserve interrupt waiting's SDK timeout policy; the
                    # live turn's explicit read timeout belongs to open_stream.
                    native_stream = await provider_call(
                        self._client.beta.sessions.events.stream(session_id=receipt.session.id)
                    )
                    stream = _NormalizedStream(native_stream, receipt.session, False)
                    async for event in stream:
                        if self._on_stop_event is not None:
                            await self._on_stop_event(event)
                        if (stop := observed_stop(event, receipt)) is not None:
                            return stop
        except TimeoutError:
            pass
        finally:
            if stream is not None:
                assert isinstance(stream, _NormalizedStream)
                await stream.aclose()
        return StopObservation(
            receipt_operation_id=receipt.operation_id,
            stopped=False,
            outcome=None,
            observed_at=datetime.now(UTC),
        )
