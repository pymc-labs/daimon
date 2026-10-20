"""Wire fixtures validate both ends of the harness boundary without an SDK."""

import pytest
from mux.drivers.selfhosted.wire import DATA_MODELS, Event, TurnRequest
from pydantic import ValidationError

DATA = {
    "status_running": {},
    "agent_message_delta": {"text": "hello"},
    "agent_message": {"parts": [{"type": "text", "text": "hello"}]},
    "tool_use": {"call_id": "c", "name": "read", "input": {"path": "a"}, "executor": "agent"},
    "tool_result": {"call_id": "c", "output": "contents", "is_error": False},
    "requires_action": {
        "action_id": "a",
        "kind": "tool_confirmation",
        "call_id": "c",
        "name": "bash",
        "input": {"command": "pwd"},
    },
    "usage": {
        "request_id": "r",
        "model": "gpt-5",
        "input_tokens": 5,
        "cached_input_tokens": 0,
        "output_tokens": 2,
        "reasoning_tokens": 1,
    },
    "turn_ended": {"outcome": "completed"},
    "error": {"kind": "model", "message": "unavailable", "retryable": True},
}


@pytest.mark.parametrize("kind", DATA)
def test_event_round_trip(kind: str) -> None:
    event = Event.model_validate(
        {
            "seq": 1,
            "session_id": "s",
            "turn_id": "t",
            "at": "2026-10-20T00:00:00Z",
            "type": kind,
            "data": DATA[kind],
        }
    )
    assert Event.model_validate_json(event.model_dump_json()) == event
    assert isinstance(event.typed_data(), DATA_MODELS[event.type])


@pytest.mark.parametrize(
    "kind,data",
    [
        ("status_running", {"extra": True}),
        ("usage", {**DATA["usage"], "input_tokens": -1}),
        ("turn_ended", {"outcome": "unknown"}),
        ("agent_message", {"parts": [{"type": "artifact_ref"}]}),
    ],
)
def test_event_rejects_invalid_payload(kind: str, data: object) -> None:
    with pytest.raises(ValidationError):
        Event.model_validate(
            {
                "seq": 1,
                "session_id": "s",
                "turn_id": "t",
                "at": "2026-10-20T00:00:00Z",
                "type": kind,
                "data": data,
            }
        )


def test_request_round_trip() -> None:
    request = TurnRequest.model_validate(
        {
            "session_id": "s",
            "turn_id": "t",
            "idempotency_key": "k",
            "model": "gpt-5",
            "instructions": "help",
            "input": [
                {"type": "text", "text": "hi"},
                {"type": "image", "media_type": "image/png", "data_b64": "AA=="},
            ],
            "mcp_servers": [
                {
                    "name": "tools",
                    "url": "https://example.com/mcp",
                    "headers_env": {"Authorization": "MCP_TOKEN"},
                }
            ],
            "skills": [
                {"name": "inline", "files": {"SKILL.md": "help"}},
                {"name": "remote", "url": "https://example.com/SKILL.md"},
            ],
            "limits": {"max_tool_calls": 5, "deadline_s": 60},
        }
    )
    assert TurnRequest.model_validate_json(request.model_dump_json()) == request
    assert request.mcp_servers[0].headers_env == {"Authorization": "MCP_TOKEN"}
