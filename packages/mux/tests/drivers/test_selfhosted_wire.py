"""Wire fixtures validate both ends of the harness boundary without an SDK."""

import pytest
from mux.drivers.selfhosted.wire import DATA_MODELS, Event, TurnRequest
from pydantic import ValidationError

DATA = {
    "model_request_started": {"request_id": "r", "model": "gpt-5"},
    "status_running": {},
    "agent_message_delta": {"message_id": "msg-1", "text": "hello"},
    "agent_message": {"message_id": "msg-1", "parts": [{"type": "text", "text": "hello"}]},
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


def test_forward_compatible_event_and_response_fields() -> None:
    from mux.drivers.selfhosted.wire import CancelResponse, EventsResponse, HealthResponse

    event = Event.model_validate(
        {
            "seq": 1,
            "session_id": "s",
            "turn_id": "t",
            "at": "2026-10-20T00:00:00Z",
            "type": "usage",
            "data": {**DATA["usage"], "future": True},
            "future_envelope": "ignored",
        }
    )
    assert event.typed_data().model_dump()["input_tokens"] == 5
    assert HealthResponse.model_validate({"version": "sha", "future": 1}).ok
    assert CancelResponse.model_validate({"cancelled": True, "future": 1}).cancelled
    assert EventsResponse.model_validate(
        {"events": [event.model_dump()], "complete": True, "future": 1}
    ).complete


def test_requests_remain_closed_and_invocations_round_trip() -> None:
    from mux.drivers.selfhosted.wire import ActionRequest, InvocationEnvelope, TurnAccepted

    envelope = InvocationEnvelope.model_validate(
        {
            "c3": {
                "method": "GET",
                "path": "/v0/sessions/s/events",
                "query": {"after_seq": 2},
                "body": None,
            }
        }
    )
    assert InvocationEnvelope.model_validate_json(envelope.model_dump_json()) == envelope
    assert (
        TurnAccepted.model_validate({"accepted": True, "turn_id": "t", "future": 1}).turn_id == "t"
    )
    with pytest.raises(ValidationError):
        ActionRequest.model_validate({"action_id": "a", "decision": "allow", "future": 1})
    with pytest.raises(ValidationError):
        InvocationEnvelope.model_validate(
            {"c3": {"method": "GET", "path": "/v0/healthz", "unexpected": True}}
        )


def test_errored_turn_requires_error_details() -> None:
    from mux.drivers.selfhosted.wire import TurnEndedData

    with pytest.raises(ValidationError, match="requires error details"):
        TurnEndedData(outcome="errored")
    assert (
        TurnEndedData.model_validate(
            {"outcome": "errored", "error": {"kind": "usage_missing", "message": "Missing usage"}}
        ).error
        is not None
    )


@pytest.mark.parametrize(
    "body",
    [
        {"action_id": "a", "decision": "allow"},
        {"action_id": "a", "decision": "deny"},
        {"action_id": "a", "decision": "result", "result": None},
        {"action_id": "a", "decision": "result", "result": {"ok": True}},
    ],
)
def test_action_decision_round_trip(body: dict[str, object]) -> None:
    from mux.drivers.selfhosted.wire import ActionRequest

    action = ActionRequest.model_validate(body)
    assert action.model_dump(mode="json") == body
    assert ActionRequest.model_validate_json(action.model_dump_json()) == action


@pytest.mark.parametrize(
    "body",
    [
        {"action_id": "a", "decision": "allow", "result": None},
        {"action_id": "a", "decision": "deny", "result": "denied"},
        {"action_id": "a", "decision": "result"},
    ],
)
def test_action_decision_rejects_ambiguous_pairing(body: dict[str, object]) -> None:
    from mux.drivers.selfhosted.wire import ActionRequest

    with pytest.raises(ValidationError, match="result must be present exactly"):
        ActionRequest.model_validate(body)


def test_binary_skill_digest_covers_name_paths_and_decoded_content() -> None:
    import base64
    import hashlib

    from mux.drivers.selfhosted.wire import InlineSkill, skill_sha256

    skill = InlineSkill(
        name="marimo_notebooks",
        files={"SKILL.md": "hello"},
        binary_files={"assets/font.bin": base64.b64encode(b"\x00\xff").decode()},
    )
    canonical = (
        b'{"files":{"SKILL.md":"aGVsbG8=","assets/font.bin":"AP8="},"name":"marimo_notebooks"}'
    )
    assert skill.sha256 == hashlib.sha256(canonical).hexdigest()
    assert skill.sha256 == skill_sha256(skill.name, skill.decoded_files())
    assert InlineSkill.model_validate_json(skill.model_dump_json()) == skill
    # Text/binary representation and insertion order do not alter the digest.
    assert (
        InlineSkill(
            name=skill.name,
            files={},
            binary_files={"assets/font.bin": "AP8=", "SKILL.md": "aGVsbG8="},
        ).sha256
        == skill.sha256
    )
    assert (
        InlineSkill(name="other", files=skill.files, binary_files=skill.binary_files).sha256
        != skill.sha256
    )
    with pytest.raises(ValidationError, match="sha256"):
        InlineSkill(name="a", files={}, sha256="0" * 64)
    with pytest.raises(ValidationError, match="sha256"):
        InlineSkill(name="a", files={}, sha256="")


@pytest.mark.parametrize("path", ["../x", "/x", "a/../x", "a//x", "./x", "a/", "a\\x", "", "x\x00"])
def test_skill_paths_cannot_escape_or_alias(path: str) -> None:
    from mux.drivers.selfhosted.wire import InlineSkill

    with pytest.raises(ValidationError, match="POSIX"):
        InlineSkill(name="safe", files={path: "x"})


def test_skill_rejects_ambiguous_paths_invalid_base64_and_unsafe_names() -> None:
    from mux.drivers.selfhosted.wire import InlineSkill

    for fields in [
        {"files": {"a": "x"}, "binary_files": {"a": "eA=="}},
        {"files": {"a": "x", "a/b": "y"}},
        {"files": {}, "binary_files": {"a": "invalid!"}},
    ]:
        with pytest.raises(ValidationError):
            InlineSkill(name="safe", **fields)
    with pytest.raises(ValidationError, match="name"):
        InlineSkill(name="../escape", files={})


def test_skill_and_turn_decoded_byte_caps() -> None:
    from mux.drivers.selfhosted.wire import SKILL_MAX_BYTES, InlineSkill

    content = "a" * SKILL_MAX_BYTES
    admitted = InlineSkill(name="a", files={"SKILL.md": content})
    with pytest.raises(ValidationError, match="8 MiB"):
        InlineSkill(name="a", files={"SKILL.md": content + "a"})
    body = {
        "session_id": "s",
        "turn_id": "t",
        "idempotency_key": "k",
        "model": "m",
        "instructions": "",
        "input": [],
        "mcp_servers": [],
        "limits": {"max_tool_calls": 1, "deadline_s": 1},
        "skills": [
            admitted.model_dump()
            | {"name": str(i), "sha256": InlineSkill(name=str(i), files=admitted.files).sha256}
            for i in range(3)
        ],
    }
    assert len(TurnRequest.model_validate(body).skills) == 3
    body["skills"].append(InlineSkill(name="extra", files={"SKILL.md": "x"}).model_dump())
    with pytest.raises(ValidationError, match="24 MiB"):
        TurnRequest.model_validate(body)


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("agent_message_delta", {"text": "hello"}),
        ("agent_message", {"parts": []}),
    ],
)
def test_assistant_messages_require_stable_identity(kind, payload):
    from mux.drivers.selfhosted.wire import DATA_MODELS

    with pytest.raises(ValidationError, match="message_id"):
        DATA_MODELS[kind].model_validate(payload)
    parsed = DATA_MODELS[kind].model_validate({**payload, "message_id": "stable", "future": 1})
    assert parsed.model_dump()["message_id"] == "stable"


def test_per_tool_policy_defaults_to_auto_and_validates_choices():
    from mux.drivers.selfhosted.wire import TurnRequest

    fields = dict(
        session_id="s",
        turn_id="t",
        idempotency_key="k",
        model="m",
        instructions="",
        input=[],
        mcp_servers=[],
        skills=[],
        limits={"max_tool_calls": 1, "deadline_s": 1},
    )
    assert TurnRequest.model_validate(fields).tool_policy == {}
    assert (
        TurnRequest.model_validate(
            {**fields, "tool_policy": {"write": "ask", "bash": "auto"}}
        ).tool_policy["write"]
        == "ask"
    )
    with pytest.raises(ValidationError):
        TurnRequest.model_validate({**fields, "tool_policy": {"write": "allow"}})
