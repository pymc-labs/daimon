"""Turn input, stream normalization, saved-state recovery and cancel truth."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from mux.contracts.actions import InputEvent, UserMessage
from mux.contracts.events import (
    Event,
    HistoryGapPayload,
    NativeProvenance,
    ReconciledPayload,
    RequiresActionPayload,
)
from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope
from mux.contracts.receipts import CancelReceipt, SendReceipt, StopObservation
from mux.contracts.resources import ProjectionSnapshot
from mux.drivers.openai._common import Context, objects, owned, text
from mux.drivers.openai.actions import translate
from mux.drivers.openai.normalize import SCHEMA_DATE, EventNormalizer
from mux.drivers.openai.sessions import OpenAISessions
from mux.drivers.openai.transport import Object, object_json, segment
from mux.errors import ProviderError


class RecoveryJournal(Protocol):
    """Host-owned atomic snapshot publication; no partial pagination commits."""

    async def read(self, session: ResourceRef) -> tuple[Event, ...] | None: ...
    async def replace(self, session: ResourceRef, events: tuple[Event, ...]) -> None: ...


class MemoryRecoveryJournal:
    """Offline journal. Retain this object when reconstructing the driver."""

    def __init__(self) -> None:
        self._snapshots: dict[ResourceRef, tuple[Event, ...]] = {}

    async def read(self, session: ResourceRef) -> tuple[Event, ...] | None:
        return self._snapshots.get(session)

    async def replace(self, session: ResourceRef, events: tuple[Event, ...]) -> None:
        self._snapshots[session] = events


async def close(source: AsyncIterator[Object]) -> None:
    method = getattr(source, "aclose", None)
    if method is not None:
        await method()


class _NormalizedStream(AsyncIterator[Event]):
    def __init__(self, source: AsyncIterator[Object], session: ResourceRef, previews: bool) -> None:
        self._source, self._previews = source, previews
        self._iterator = source.__aiter__()
        self._normalizer = EventNormalizer(session.id)
        self._closed = False

    def __aiter__(self) -> _NormalizedStream:
        return self

    async def __anext__(self) -> Event:
        if self._closed:
            raise StopAsyncIteration
        try:
            while True:
                raw = await self._iterator.__anext__()
                event = self._normalizer.normalize(raw)
                if event is not None and (self._previews or event.authority != "preview"):
                    return event
        except (ValueError, KeyError, TypeError):
            await self.aclose()
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_event"
            ) from None
        except BaseException:
            await self.aclose()
            raise

    @owned
    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await close(self._source)


def event_identity(event: Event) -> tuple[str, str]:
    if event.item_id is not None:
        return ("item", event.item_id)
    if event.type == "session.turn_ended":
        return ("turn", event.turn_id or event.id)
    return ("event", event.id)


def final_items(items: Sequence[Object]) -> tuple[Object, ...]:
    """Deduplicate overlapping snapshot pages without regressing final items."""
    saved: dict[str, Object] = {}
    for item in items:
        id_ = text(item["id"])
        old = saved.get(id_)
        if old is None or old.get("status") not in ("completed", "incomplete", "failed"):
            saved[id_] = item
    return tuple(saved.values())


def merge_history(previous: Sequence[Event], incoming: Sequence[Event]) -> tuple[Event, ...]:
    """Keep materialized cursor identities and immutable root outcomes."""
    merged = list(previous)
    identities = {event_identity(event): i for i, event in enumerate(merged)}
    for event in incoming:
        identity = event_identity(event)
        index = identities.get(identity)
        if index is None:
            identities[identity] = len(merged)
            merged.append(event)
            continue
        old = merged[index]
        if old.type == "session.turn_ended":
            if old.payload["outcome"] != event.payload["outcome"]:
                raise ValueError("conflicting root outcomes")
            continue
        if old.authority != "preview" and event.authority == "preview":
            continue
        if (old.type, old.payload, old.turn_id, old.thread_id) != (
            event.type,
            event.payload,
            event.turn_id,
            event.thread_id,
        ):
            merged[index] = event.model_copy(update={"id": old.id})
    return tuple(event.model_copy(update={"sequence": i}) for i, event in enumerate(merged))


class OpenAIEvents:
    """The host serializes turns and owns durable operation intent/leases.

    HTTP 202 is queued acceptance with no input IDs. Never manufacture a
    processed receipt. Uncertain delivery stays unknown and is not resent.
    Native input mode/cancel target prechecks are observations, not atomic CAS;
    expected_turn is refused until a documented native precondition exists.
    """

    def __init__(
        self, context: Context, sessions: OpenAISessions, journal: RecoveryJournal
    ) -> None:
        self._c, self._sessions, self._journal = context, sessions, journal

    @owned
    async def send(
        self,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None = None,
    ) -> SendReceipt:
        self._c.check(scope, session, "session")
        if expected_turn is not None:
            raise self._c.unsupported("expected_turn")
        if not events:
            raise ProviderError("invalid_request", retryable=False, native_code="empty_input")
        current = await self._sessions.retrieve(scope, session)
        if current.state == "terminated":
            raise self._c.unsupported("session_not_accepting_input")
        raw: Object = {
            "status": "in_progress" if current.state == "running" else current.state,
            "required_actions": [dict(action.payload) for action in current.required_actions],
        }
        inputs = translate(events, raw, self._c.profile_id)
        try:
            await self._c.call(
                "POST",
                f"/agents/sessions/{segment(session.id)}/events",
                body={"events": inputs},
                key=key,
            )
        except ProviderError as error:
            if error.category == "transient_network":
                return SendReceipt(operation_id=key, status="outcome_unknown", input_ids=())
            raise
        return SendReceipt(operation_id=key, status="queued", input_ids=())

    @owned
    async def open_stream(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        after: str | None = None,
        previews: bool = False,
    ) -> AsyncIterator[Event]:
        self._c.check(scope, session, "session")
        if after is not None:
            raise self._c.unsupported("native_event_replay")
        source = await self._c.transport.open_stream(
            f"/agents/sessions/{segment(session.id)}/events"
        )
        return _NormalizedStream(source, session, previews)

    async def stream(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        after: str | None = None,
        previews: bool = False,
    ) -> AsyncIterator[Event]:
        source = await self.open_stream(scope, session, after=after, previews=previews)
        try:
            async for event in source:
                yield event
        finally:
            if isinstance(source, _NormalizedStream):
                await source.aclose()

    async def _pages(self, session: ResourceRef, resource: str) -> list[Object]:
        result: list[Object] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            params: dict[str, str | int] = {"order": "asc", "limit": 100}
            if cursor is not None:
                params["after"] = cursor
            raw = await self._c.call(
                "GET", f"/agents/sessions/{segment(session.id)}/{resource}", params=params
            )
            result.extend(objects(raw["data"]))
            if raw["has_more"] is False:
                return result
            if raw["has_more"] is not True:
                raise ValueError("invalid has_more")
            cursor = text(raw["last_id"])
            if cursor in seen:
                raise ValueError("pagination cursor loop")
            seen.add(cursor)

    @owned
    async def reconcile(self, scope: Scope, session: ResourceRef) -> ProjectionSnapshot:
        self._c.check(scope, session, "session")
        previous = await self._journal.read(session) or ()
        normalizer = EventNormalizer(session.id, prior=previous)
        source = await self._c.transport.open_stream(
            f"/agents/sessions/{segment(session.id)}/events"
        )
        buffered: list[Object] = []
        disconnected = False

        async def buffer() -> None:
            nonlocal disconnected
            try:
                async for raw in source:
                    if len(buffered) >= 10000:
                        raise ProviderError(
                            "upstream", retryable=True, native_code="recovery_buffer_full"
                        )
                    buffered.append(raw)
            except ProviderError:
                disconnected = True
                raise
            disconnected = True

        task = asyncio.create_task(buffer())
        try:
            # Start consuming before the first snapshot read. Neither a partial
            # snapshot nor a disconnect during pagination can publish success.
            await asyncio.sleep(0)
            current = await self._sessions.retrieve(scope, session)
            turns = await self._pages(session, "turns")
            items = await self._pages(session, "items")
            await asyncio.sleep(0)
            if task.done():
                task.result()
            if disconnected:
                raise ProviderError(
                    "transient_network", retryable=True, native_code="snapshot_stream_disconnected"
                )
            events: list[Event] = []
            for turn in turns:
                event = normalizer.saved_turn(turn)
                if event is not None:
                    events.append(event)
            for item in final_items(items):
                event = normalizer.saved_item(item)
                if event is not None:
                    events.append(event)
            for raw in buffered:
                event = normalizer.normalize(raw)
                if event is not None and event.authority != "preview":
                    events.append(event)
            gap = "missed_native_events"
            snapshot_id = uuid4().hex
            for id_, kind, payload, authority in (
                (
                    "openai:gap:" + session.id + ":" + snapshot_id,
                    "session.history_gap",
                    HistoryGapPayload(domain="native_events", recoverable=False),
                    "gap",
                ),
                (
                    "openai:reconciled:" + session.id + ":" + snapshot_id,
                    "session.reconciled",
                    ReconciledPayload(
                        snapshot_ref=session.id, coverage="saved_items_and_turns", gaps=(gap,)
                    ),
                    "reconciled",
                ),
            ):
                events.append(
                    Event(
                        id=id_,
                        session_id=session.id,
                        sequence=len(events),
                        type=kind,
                        observed_at=datetime.now(UTC),
                        authority="gap" if authority == "gap" else "reconciled",
                        payload=object_json(payload.model_dump(mode="json")),
                        native=NativeProvenance(provider="openai", api_revision=SCHEMA_DATE),
                    )
                )
            # Native status can change while listing. Buffered root events override
            # the earlier snapshot; EOF/idle/subagent end cannot override it.
            state, active = current.state, current.active_root_turn
            actions = current.required_actions
            if active is not None and normalizer.terminal(active):
                if state != "terminated":
                    state = "idle"
                active, actions = None, ()
            for event in events:
                if event.type == "session.turn_ended" and event.turn_id == active:
                    if state != "terminated":
                        state = "idle"
                    active, actions = None, ()
                elif event.type == "session.status_running" and state != "terminated":
                    if not (
                        state == "requires_action"
                        and active == event.turn_id
                        and event.authority == "reconciled"
                    ):
                        state, active, actions = "running", event.turn_id, ()
                elif event.type == "session.requires_action" and state != "terminated":
                    payload = event.typed_payload()
                    if not isinstance(payload, RequiresActionPayload):
                        raise ValueError("invalid required-action payload")
                    active = event.turn_id or active
                    if active is None:
                        raise ProviderError(
                            "upstream", retryable=True, native_code="active_root_unavailable"
                        )
                    state, actions = "requires_action", payload.actions
                elif event.type == "session.status_terminated":
                    state, active, actions = "terminated", None, ()
            if state == "provisioning":
                raise ProviderError("upstream", retryable=True, native_code="snapshot_provisioning")
            published = merge_history(previous, events)
            projection = ProjectionSnapshot(
                session=session,
                cursor=published[-1].id,
                state=state,
                active_root_turn=active,
                required_actions=actions,
                gaps=(gap,),
                taken_at=datetime.now(UTC),
            )
            await self._journal.replace(session, published)
            return projection
        except (ValueError, KeyError, TypeError):
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_snapshot"
            ) from None
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError, ProviderError):
                await task
            await close(source)

    @owned
    async def list(self, scope: Scope, session: ResourceRef, *, page: PageRequest) -> Page[Event]:
        self._c.check(scope, session, "session")
        previous = await self._journal.read(session) or ()
        normalizer = EventNormalizer(session.id, prior=previous)
        values = [normalizer.saved_turn(turn) for turn in await self._pages(session, "turns")]
        values.extend(
            normalizer.saved_item(item) for item in final_items(await self._pages(session, "items"))
        )
        saved = merge_history(previous, [event for event in values if event is not None])
        await self._journal.replace(session, saved)
        ordered = saved[::-1] if page.order == "desc" else saved
        start = 0
        if page.cursor is not None:
            start = next((i + 1 for i, event in enumerate(ordered) if event.id == page.cursor), -1)
            if start < 0:
                raise ProviderError(
                    "invalid_request", retryable=False, native_code="unknown_history_cursor"
                )
        limit = page.limit or 100
        data = ordered[start : start + limit]
        more = start + len(data) < len(ordered)
        return Page(data=data, has_more=more, next_cursor=data[-1].id if more else None)

    @owned
    async def cancel(
        self, scope: Scope, session: ResourceRef, *, turn_id: str, key: str
    ) -> CancelReceipt:
        self._c.check(scope, session, "session")
        current = await self._sessions.retrieve(scope, session)
        now = datetime.now(UTC)
        status = "requested"
        if current.active_root_turn != turn_id:
            turn = await self._c.call(
                "GET", f"/agents/sessions/{segment(session.id)}/turns/{segment(turn_id)}"
            )
            if turn.get("id") != turn_id or turn.get("session_id") != session.id:
                raise ProviderError("upstream", retryable=False, native_code="wrong_root_turn")
            if (
                turn.get("subagent_id") is None
                and "subagent_id" in turn
                and turn.get("status") in ("completed", "cancelled", "failed")
            ):
                status = "already_stopped"
            else:
                raise self._c.unsupported("cancel_target")
        else:
            try:
                await self._c.call(
                    "POST",
                    f"/agents/sessions/{segment(session.id)}/events",
                    body={
                        "events": [{"type": "agent.session.input.cancel"}],
                    },
                    key=key,
                )
            except ProviderError as error:
                if error.category != "transient_network":
                    raise
                status = "outcome_unknown"
        return CancelReceipt.model_validate(
            {
                "operation_id": key,
                "session": session,
                "turn_id": turn_id,
                "status": status,
                "requested_at": now,
            }
        )

    @owned
    async def wait_stopped(
        self, scope: Scope, receipt: CancelReceipt, *, deadline: datetime
    ) -> StopObservation:
        self._c.check(scope, receipt.session, "session")
        now = datetime.now(UTC)
        if now >= deadline:
            return StopObservation(
                receipt_operation_id=receipt.operation_id, stopped=False, observed_at=now
            )
        try:
            async with asyncio.timeout((deadline - now).total_seconds()):
                raw = await self._c.call(
                    "GET",
                    f"/agents/sessions/{segment(receipt.session.id)}/turns/{segment(receipt.turn_id)}",
                )
        except TimeoutError:
            return StopObservation(
                receipt_operation_id=receipt.operation_id,
                stopped=False,
                observed_at=datetime.now(UTC),
            )
        if (
            raw.get("id") != receipt.turn_id
            or raw.get("session_id") != receipt.session.id
            or raw.get("subagent_id") is not None
            or "subagent_id" not in raw
        ):
            raise ProviderError("upstream", retryable=False, native_code="wrong_root_turn")
        outcomes = {"completed": "completed", "failed": "errored", "cancelled": "interrupted"}
        return StopObservation.model_validate(
            {
                "receipt_operation_id": receipt.operation_id,
                "stopped": raw.get("status") in outcomes,
                "outcome": outcomes.get(text(raw["status"])),
                "observed_at": datetime.now(UTC),
            }
        )

    @owned
    async def steer(
        self,
        scope: Scope,
        session: ResourceRef,
        message: UserMessage,
        *,
        active_turn: str,
        key: str,
    ) -> SendReceipt:
        self._c.check(scope, session, "session")
        current = await self._sessions.retrieve(scope, session)
        if current.active_root_turn != active_turn:
            raise self._c.unsupported("steer_target")
        return await self.send(
            scope, session, (message.model_copy(update={"mode": "steer"}),), key=key
        )
