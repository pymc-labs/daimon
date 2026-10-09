"""Final scope cut: free-form event mappings never become recording evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from mux.conformance.recording import Recorder, RecordingError, Replay
from mux.conformance.test_recording import delta, request, save
from mux.contracts.events import Event

KEY = "sk-" + "f" * 40


def tool_event(input_value: dict[str, JsonValue]) -> Event:
    return Event.model_validate(
        {
            **delta("safe").model_dump(),
            "type": "agent.tool_use",
            "authority": "record",
            "payload": {
                "call_id": "call",
                "tool_name": "tool",
                "input": input_value,
                "executor": "host",
            },
        }
    )


def action_event(input_value: dict[str, JsonValue]) -> Event:
    return Event.model_validate(
        {
            **delta("safe").model_dump(),
            "type": "session.requires_action",
            "authority": "record",
            "payload": {
                "actions": [{"id": "action", "kind": "tool_confirmation", "payload": input_value}]
            },
        }
    )


@pytest.mark.parametrize("explicit", (False, True))
@pytest.mark.parametrize("kind", ("tool", "action"))
async def test_review_key_fragment_mapping_is_omitted_not_written(
    tmp_path: Path, explicit: bool, kind: str
) -> None:
    # Exact REVIEW-QA-PR5-8830540 key split; neither fragment alone hits the audit.
    fragments: dict[str, JsonValue] = {KEY[:22]: "left", KEY[22:]: "right"}
    assert "".join(fragments) == KEY
    event = tool_event(fragments) if kind == "tool" else action_event(fragments)
    secrets = (KEY,) if explicit else ()
    recorder = Recorder(secrets=secrets)
    recorder.record(request(), (event,))
    path = tmp_path / "omitted.json"
    save(recorder, path)
    stored = path.read_text()
    assert KEY[:22] not in stored and KEY[22:] not in stored
    assert "left" not in stored and "right" not in stored
    assert '"input_omitted":true' in stored
    replay = Replay.load(path, secrets=secrets)
    observed = (await replay.events(request()))[0]
    payload = observed.typed_payload()
    assert payload is not None
    value = payload.model_dump(mode="json")
    if kind == "tool":
        assert value["tool_name"] == "tool"
        assert value["input"] == {"input_omitted": True}
        assert event.payload["input"] == fragments
    else:
        assert value["actions"][0]["payload"] == {"input_omitted": True}
    replay.finish()


@pytest.mark.parametrize("explicit", (False, True))
@pytest.mark.parametrize("kind", ("tool", "action"))
def test_export_and_replay_refuse_injected_key_fragment_mappings(
    tmp_path: Path, explicit: bool, kind: str
) -> None:
    fragments: dict[str, JsonValue] = {KEY[:22]: "left", KEY[22:]: "right"}
    event = tool_event(fragments) if kind == "tool" else action_event(fragments)
    secrets = (KEY,) if explicit else ()
    recorder = Recorder(secrets=secrets)
    recorder.record(request(), (event,))
    path = tmp_path / "safe.json"
    save(recorder, path)
    data = json.loads(path.read_text())
    payload = data["batches"][0]["events"][0]["payload"]
    if kind == "tool":
        payload["input"] = fragments
    else:
        payload["actions"][0]["payload"] = fragments
    path.write_text(json.dumps(data))
    with pytest.raises(RecordingError, match="free-form event mapping"):
        Replay.load(path, secrets=secrets)

    # Mutate the captured nested mapping: save must validate again, not trust record().
    stored_event = recorder._batches[0].events[0]  # pyright: ignore[reportPrivateUsage]
    if kind == "tool":
        mapping = stored_event.payload["input"]
    else:
        actions = stored_event.payload["actions"]
        assert isinstance(actions, list) and isinstance(actions[0], dict)
        mapping = actions[0]["payload"]
    assert isinstance(mapping, dict)
    mapping.clear()
    mapping.update(fragments)
    refused = tmp_path / "refused.json"
    with pytest.raises(RecordingError, match="free-form event mapping"):
        save(recorder, refused)
    assert not refused.exists() and not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("kind", ("tool", "action"))
@pytest.mark.parametrize(
    "input_value",
    (
        {},
        {"ordinary": "value"},
        {"input_omitted": False},
        {"input_omitted": 1},
        {"input_omitted": {"nested": True}},
        {"input_omitted": True, "extra": "value"},
    ),
)
def test_replay_accepts_only_exact_boolean_sentinel(
    tmp_path: Path, kind: str, input_value: dict[str, JsonValue]
) -> None:
    event = tool_event(input_value) if kind == "tool" else action_event(input_value)
    data = {
        "version": 2,
        "fixture_id": "C16",
        "provider": "fake",
        "model": "fake",
        "complete": True,
        "batches": [
            {
                "request": request().model_dump(mode="json"),
                "events": [event.model_dump(mode="json")],
            }
        ],
    }
    path = tmp_path / "injected.json"
    path.write_text(json.dumps(data))
    with pytest.raises(RecordingError, match="free-form event mapping"):
        Replay.load(path)


@pytest.mark.parametrize("placement", ("root", "text_part", "artifact"))
def test_mapping_outside_fixed_normalized_schema_refuses_load(
    tmp_path: Path, placement: str
) -> None:
    event = delta("safe").model_dump(mode="json")
    if placement == "root":
        event["payload"]["args"] = {"arbitrary": "value"}
    else:
        event.update(type="agent.message", authority="record")
        part = {"type": "text", "text": "safe", "args": {"arbitrary": "value"}}
        if placement == "artifact":
            part = {
                "type": "artifact_ref",
                "artifact": {
                    "id": "artifact",
                    "kind": "file",
                    "provider": "anthropic",
                    "account_scope_id": "account",
                    "args": {"arbitrary": "value"},
                },
            }
        event["payload"] = {"item_id": "message", "content": [part]}
    path = tmp_path / "injected.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "fixture_id": "C16",
                "provider": "fake",
                "model": "fake",
                "complete": True,
                "batches": [{"request": request().model_dump(mode="json"), "events": [event]}],
            }
        )
    )
    with pytest.raises(RecordingError):
        Replay.load(path)


@pytest.mark.parametrize(
    ("event_type", "payload"),
    (
        ("user.message", {"input_id": "input", "content": [{"type": "text", "text": "safe"}]}),
        (
            "agent.message",
            {
                "item_id": "message",
                "content": [
                    {
                        "type": "artifact_ref",
                        "artifact": {
                            "id": "file",
                            "kind": "file",
                            "provider": "anthropic",
                            "account_scope_id": "account",
                        },
                    }
                ],
            },
        ),
        ("agent.tool_result", {"call_id": "call", "content": [{"type": "text", "text": "safe"}]}),
        ("session.status_running", {"root_turn_id": "turn"}),
        ("session.turn_ended", {"root_turn_id": "turn", "outcome": "completed"}),
        ("session.error", {"category": "auth", "retry_status": "terminal"}),
        ("session.status_terminated", {"reason": "completed"}),
        (
            "tool_server.degraded",
            {"server": "server", "error_type": "failed", "retry_status": "terminal"},
        ),
        ("usage.observed", {"observation_id": "observation", "revision": 1}),
        ("session.reconciled", {"snapshot_ref": "snapshot", "coverage": "complete", "gaps": []}),
        ("session.history_gap", {"domain": "history", "recoverable": True}),
    ),
)
async def test_fixed_payload_models_and_nested_artifacts_still_replay(
    tmp_path: Path, event_type: str, payload: dict[str, JsonValue]
) -> None:
    event = Event.model_validate(
        {
            **delta("safe").model_dump(),
            "type": event_type,
            "authority": "record",
            "payload": payload,
        }
    )
    recorder = Recorder()
    recorder.record(request(), (event,))
    path = tmp_path / "fixed.json"
    save(recorder, path)
    replay = Replay.load(path)
    assert (await replay.events(request()))[0] == event
    replay.finish()
