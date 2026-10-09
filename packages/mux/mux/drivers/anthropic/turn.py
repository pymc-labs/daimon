"""Unwired Events port over an injected SDK client. No journal or host policy."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import httpx
from anthropic import APIConnectionError, APITimeoutError, AsyncAnthropic, AsyncStream, omit
from anthropic.types.beta.sessions import BetaManagedAgentsStreamSessionEvents

from mux.contracts.actions import InputEvent
from mux.contracts.events import Event
from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope
from mux.contracts.receipts import CancelReceipt, SendReceipt, StopObservation
from mux.contracts.resources import ProjectionSnapshot
from mux.drivers.anthropic.actions import translate_inputs
from mux.drivers.anthropic.normalize import EventNormalizer, object_json
from mux.drivers.anthropic.resources._authorization import (
    ResourceAuthorization,
    authorize,
    check_ref,
)
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter
from mux.errors import ProviderError, UnsupportedCapability


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
    (the injected SDK retry policy remains the host's responsibility).
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
        authorization: ResourceAuthorization | None = None,
    ) -> None:
        self._client = client
        self._account_scope_id = account_scope_id
        self._authorization = authorization

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
                self._client.beta.sessions.events.send(session.id, events=inputs)
            )
        except ProviderError as error:
            if isinstance(error.__cause__, APIConnectionError | APITimeoutError):
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
                session.id, event_deltas=["agent.message"] if previews else omit
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
        data = tuple(
            normalizer.normalize(
                object_json(event.model_dump(mode="json")), observed_at=datetime.now(UTC)
            )
            for event in result.data
        )
        return Page(
            data=data,
            has_more=result.next_page is not None,
            next_cursor=result.next_page,
        )

    async def reconcile(self, scope: Scope, session: ResourceRef) -> ProjectionSnapshot:
        self._check(scope, session)
        raise UnsupportedCapability(("reconcile",), "anthropic.managed_agents")

    async def cancel(
        self, scope: Scope, session: ResourceRef, *, turn_id: str, key: str
    ) -> CancelReceipt:
        self._check(scope, session)
        requested_at = datetime.now(UTC)
        try:
            await provider_call(
                self._client.beta.sessions.events.send(
                    session.id, events=[{"type": "user.interrupt"}]
                )
            )
        except ProviderError as error:
            if isinstance(error.__cause__, APIConnectionError | APITimeoutError):
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

    async def wait_stopped(
        self, scope: Scope, receipt: CancelReceipt, *, deadline: datetime
    ) -> StopObservation:
        self._check(scope, receipt.session)
        # Stop reconciliation and interrupt truth are the next cancellation PR;
        # never convert a request acknowledgment into a stop observation.
        raise UnsupportedCapability(("wait_stopped",), "anthropic.managed_agents")
