"""Self-hosted neutral events use the existing host reducers without provider I/O."""

from datetime import UTC, datetime

import pytest
from daimon.core.turn.selfhosted_codec import display_batch, tool_display_id
from mux.contracts.events import Event, NativeProvenance, RequiredAction, RequiresActionPayload
from mux.errors import ScopeViolation


def event(type_, payload, provider="selfhosted"):
    return Event(
        id="event",
        session_id="session",
        sequence=1,
        type=type_,
        turn_id="turn",
        authority="record",
        observed_at=datetime.now(UTC),
        payload=payload,
        native=NativeProvenance(provider=provider, api_revision="daimon-harness/0"),
    )


def test_tool_confirmation_displays_exact_tool_input_and_recoverable_identity():
    value = event(
        "session.requires_action",
        RequiresActionPayload(
            actions=(
                RequiredAction(
                    id="approval",
                    kind="tool_confirmation",
                    call_id="call",
                    payload={"name": "bash", "input": {"command": "echo hi"}},
                ),
            )
        ).model_dump(mode="json"),
    )
    tool, pause = display_batch(value, session_id="session")
    assert tool.type == "agent.tool_use"
    assert tool.id == tool_display_id("approval")
    assert tool.name == "bash"
    assert tool.input == {"command": "echo hi"}
    assert tool.evaluated_permission == "ask"
    assert pause.type == "session.status_idle"
    assert pause.stop_reason.type == "requires_action"
    assert pause.stop_reason.event_ids == [tool.id]


def test_usage_failure_remains_neutral_and_terminal_drives_error_path():
    value = event(
        "session.error",
        {"category": "upstream", "retry_status": "terminal", "native_code": "usage_unavailable"},
    )
    assert display_batch(value, session_id="session") == ()


def test_foreign_provider_is_refused():
    value = event("session.status_running", {"root_turn_id": "turn"}, provider="openai")
    with pytest.raises(ScopeViolation):
        display_batch(value, session_id="session")
