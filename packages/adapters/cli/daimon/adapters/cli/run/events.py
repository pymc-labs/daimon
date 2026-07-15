"""NDJSON envelope definitions and TurnState serializer for `daimon run`."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Annotated, Literal, cast

from daimon.core.errors import TurnError
from daimon.core.turn.state import TurnState
from pydantic import BaseModel, Field


class _Envelope(BaseModel):
    session_id: str
    turn_id: str


class SseEvent(_Envelope):
    kind: Literal["sse"] = "sse"
    event: dict[str, object]


class ReconnectEvent(_Envelope):
    kind: Literal["reconnect"] = "reconnect"
    reason: Literal["connection_dropped"]


class RateLimitedEvent(_Envelope):
    kind: Literal["rate_limited"] = "rate_limited"
    until: datetime | None


class InterruptSentEvent(_Envelope):
    kind: Literal["interrupt_sent"] = "interrupt_sent"
    source: Literal["sigint", "cancel_event"]


class TerminalEndTurn(_Envelope):
    kind: Literal["terminal"] = "terminal"
    status: Literal["end_turn"] = "end_turn"
    state: dict[str, object]


class TerminalMaxTurns(_Envelope):
    kind: Literal["terminal"] = "terminal"
    status: Literal["max_turns"] = "max_turns"
    state: dict[str, object]


class TerminalFailed(_Envelope):
    kind: Literal["terminal"] = "terminal"
    status: Literal["failed"] = "failed"
    error: dict[str, object]
    state: dict[str, object]


class TerminalCancelled(_Envelope):
    kind: Literal["terminal"] = "terminal"
    status: Literal["cancelled"] = "cancelled"
    state: dict[str, object]


_Terminal = Annotated[
    TerminalEndTurn | TerminalMaxTurns | TerminalFailed | TerminalCancelled,
    Field(discriminator="status"),
]

RunStreamEvent = SseEvent | ReconnectEvent | RateLimitedEvent | InterruptSentEvent | _Terminal


def serialize_event(event: RunStreamEvent) -> str:
    return event.model_dump_json()


def _dump(obj: object) -> object:
    """Recursive JSON-compatible dump.

    `TurnState` is a frozen dataclass containing dataclasses and SDK
    pydantic models (via `result_content`/`stop_reason`) that pydantic's
    `TypeAdapter` can't introspect because `TurnError` is a plain
    Exception subclass. We handle each shape explicitly.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, TurnError):
        return {
            "kind": obj.kind,
            "message": obj.message,
            "cause": None if obj.cause is None else str(obj.cause),
        }
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _dump(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple, frozenset, set)):
        return [_dump(x) for x in cast(list[object], obj)]
    if isinstance(obj, dict):
        return {str(k): _dump(v) for k, v in cast(dict[object, object], obj).items()}
    return str(obj)


def serialize_turn_state(state: TurnState) -> dict[str, object]:
    """JSON-compatible dump of `TurnState`; stringifies `error.cause`."""
    return cast(dict[str, object], _dump(state))
