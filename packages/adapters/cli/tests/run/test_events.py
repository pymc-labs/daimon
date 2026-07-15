"""NDJSON envelope + TurnState serializer."""

from __future__ import annotations

import json

from daimon.adapters.cli.run.events import (
    ReconnectEvent,
    RunStreamEvent,
    SseEvent,
    TerminalFailed,
    serialize_event,
    serialize_turn_state,
)
from daimon.core.errors import TurnError
from daimon.core.turn.state import TurnState


def test_sse_event_serializes_with_kind_field() -> None:
    ev: RunStreamEvent = SseEvent(
        session_id="sess_1", turn_id="turn_1", event={"type": "message_start"}
    )
    line = serialize_event(ev)
    parsed = json.loads(line)
    assert parsed["kind"] == "sse"
    assert parsed["event"]["type"] == "message_start"


def test_reconnect_event_accepts_connection_dropped() -> None:
    ev = ReconnectEvent(session_id="s", turn_id="t", reason="connection_dropped")
    assert json.loads(serialize_event(ev))["reason"] == "connection_dropped"


def test_terminal_failed_carries_error_object() -> None:
    ev = TerminalFailed(
        session_id="s",
        turn_id="t",
        error={"kind": "upstream", "message": "boom"},
        state={"content": []},
    )
    parsed = json.loads(serialize_event(ev))
    assert parsed["error"] == {"kind": "upstream", "message": "boom"}


def test_serialize_turn_state_handles_empty_state() -> None:
    state = TurnState()
    dumped = serialize_turn_state(state)
    round_tripped = json.loads(json.dumps(dumped))
    assert "content" in round_tripped


def test_serialize_turn_state_stringifies_error_cause() -> None:
    cause = RuntimeError("simulated upstream failure")
    state = TurnState(error=TurnError(kind="upstream", message="boom", cause=cause))
    dumped = serialize_turn_state(state)
    assert dumped["error"]["cause"] == "simulated upstream failure"
    json.dumps(dumped)


def test_serialize_turn_state_handles_none_error_cause() -> None:
    state = TurnState(error=TurnError(kind="reducer_bug", message="x", cause=None))
    dumped = serialize_turn_state(state)
    assert dumped["error"]["cause"] is None
