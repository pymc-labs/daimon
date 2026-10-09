"""Defense in depth for the written normalized schema, with synthetic keys only."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from pydantic import JsonValue

from mux.conformance.recording import Recorder, RecordingError, Replay
from mux.conformance.test_recording import delta, request, save
from mux.contracts.events import Event

KEYS = ("sk-" + "f" * 40, "sk-proj-" + "f" * 40, "AIza" + "g" * 40)


def encode(value: str, encoding: str) -> str:
    if encoding == "url":
        return "".join(f"%{ord(char):02x}" for char in value)
    if encoding == "unicode":
        return "".join(f"\\u{ord(char):04x}" for char in value)
    if encoding == "hex":
        return "".join(f"\\x{ord(char):02x}" for char in value)
    if encoding.startswith("base64"):
        codec = "utf-16-le" if encoding.endswith("utf16") else "utf-8"
        return base64.b64encode(value.encode(codec)).decode()
    return value


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("neighbour", ("", "word", "_", "9", r"\n", r"\t", r"\""))
@pytest.mark.parametrize("encoding", ("plain", "url", "unicode", "hex", "base64", "base64utf16"))
def test_glued_and_encoded_key_events_refuse_before_any_disk_write(
    tmp_path: Path, key: str, neighbour: str, encoding: str
) -> None:
    recorder = Recorder()
    path = tmp_path / "unsafe.json"
    with pytest.raises(RecordingError):
        recorder.record(request(), (delta(neighbour + encode(key, encoding)),))
        save(recorder, path)
    assert not path.exists() and not list(tmp_path.glob("*.tmp"))
    # Catching a rejection cannot let a caller export the earlier safe prefix.
    with pytest.raises(RecordingError):
        save(recorder, path)


@pytest.mark.parametrize("key", (*KEYS, "opaque-dummy-credential"))
@pytest.mark.parametrize("encoding", ("plain", "unicode", "base64", "base64utf16"))
@pytest.mark.parametrize("batching", ("same", "separate"))
def test_keys_split_across_normalized_events_refuse_export_and_injected_replay(
    tmp_path: Path, key: str, encoding: str, batching: str
) -> None:
    secrets = (key,) if key.startswith("opaque") else ()
    texts = (encode(key[:9], encoding), encode(key[9:], encoding))
    events = tuple(delta(text, index) for index, text in enumerate(texts))
    recorder = Recorder(secrets=secrets)
    path = tmp_path / "unsafe.json"
    with pytest.raises(RecordingError):
        if batching == "same":
            recorder.record(request(), events)
        else:
            for event in events:
                recorder.record(request(), (event,))
        save(recorder, path)
    assert not path.exists()
    # Bypass the recorder to check exactly the same load-time audit.
    value = {
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
            for event in events
        ],
    }
    path.write_text(json.dumps(value))
    with pytest.raises(RecordingError):
        Replay.load(path, secrets=secrets)


@pytest.mark.parametrize("text", ("a basic understanding", "risk-adjusted", "ordinary output"))
async def test_benign_normalized_prose_still_replays(tmp_path: Path, text: str) -> None:
    recorder = Recorder()
    recorder.record(request(), (delta(text),))
    path = tmp_path / "safe.json"
    save(recorder, path)
    replay = Replay.load(path)
    assert (await replay.events(request()))[0].payload["text"] == text
    replay.finish()


@pytest.mark.parametrize(
    "raw", (b'data: {"text":"private"}\n\n', "private HTTP body", {"response": "private"})
)
def test_raw_responses_are_not_accepted_by_event_recorder(tmp_path: Path, raw: object) -> None:
    recorder = Recorder()
    with pytest.raises(RecordingError):
        recorder.record(request(), (raw,))  # pyright: ignore[reportArgumentType]
    with pytest.raises(RecordingError):
        save(recorder, tmp_path / "raw.json")
    assert not list(tmp_path.iterdir())
    assert not hasattr(recorder, "call")


@pytest.mark.parametrize("kind", ("open_event", "native_part", "inline_binary", "native_action"))
def test_opaque_native_escape_hatches_refuse_recording(tmp_path: Path, kind: str) -> None:
    value = delta("normal").model_dump(mode="json")
    value["authority"] = "record"
    if kind == "open_event":
        value.update(type="native.response", payload={"raw": "private-body"})
    elif kind == "native_action":
        value.update(
            type="session.requires_action",
            payload={
                "actions": [{"id": "action", "kind": "native", "payload": {"raw": "private-body"}}]
            },
        )
    else:
        part = (
            {
                "type": "native",
                "namespace": "fake",
                "version": 1,
                "payload": {"raw": "private-body"},
            }
            if kind == "native_part"
            else {"type": "image", "media_type": "image/png", "data_base64": "cHJpdmF0ZQ=="}
        )
        value.update(type="agent.message", payload={"item_id": "message", "content": [part]})
    event = Event.model_validate(value)
    recorder = Recorder()
    with pytest.raises(RecordingError):
        recorder.record(request(), (event,))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("field", ("record", "raw_ref"))
def test_injected_native_provenance_refuses_replay(tmp_path: Path, field: str) -> None:
    recorder = Recorder()
    recorder.record(request(), (delta("safe"),))
    path = tmp_path / "injected.json"
    save(recorder, path)
    value = json.loads(path.read_text())
    value["batches"][0]["events"][0]["native"][field] = "private-body"
    path.write_text(json.dumps(value))
    with pytest.raises(RecordingError):
        Replay.load(path)


@pytest.mark.parametrize("field", ("provider", "model", "path", "body_fields"))
def test_metadata_credentials_refuse_export(tmp_path: Path, field: str) -> None:
    recorder = Recorder()
    meta = request()
    kwargs = {"fixture_id": "C16", "provider": "fake", "model": "fake", "complete": True}
    if field in kwargs:
        kwargs[field] = KEYS[0]
    else:
        meta = meta.model_copy(update={field: "/" + KEYS[0] if field == "path" else (KEYS[0],)})
    with pytest.raises(RecordingError):
        recorder.record(meta, ())
        recorder.save(tmp_path / "unsafe.json", **kwargs)  # pyright: ignore[reportArgumentType]
    assert not list(tmp_path.iterdir())


def test_opaque_tool_input_is_omitted_and_encoded_explicit_literal_refuses(tmp_path: Path) -> None:
    event = Event.model_validate(
        {
            **delta("safe").model_dump(),
            "type": "agent.tool_use",
            "authority": "record",
            "payload": {
                "call_id": "call",
                "tool_name": "tool",
                "input": {"session_token": "opaque-secret"},
                "executor": "host",
            },
        }
    )
    recorder = Recorder()
    recorder.record(request(), (event,))
    path = tmp_path / "omitted.json"
    save(recorder, path)
    assert "opaque-secret" not in path.read_text() and "session_token" not in path.read_text()
    path.unlink()
    with pytest.raises(RecordingError):
        Recorder(secrets=("private-literal",)).record(
            request(), (delta(quote(base64.b64encode(b"private-literal").decode())),)
        )
    assert not list(tmp_path.iterdir())


def test_tape_size_limit_refuses_before_opening_a_tempfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mux.conformance.recording._MAX_TAPE_BYTES", 64)
    recorder = Recorder()
    recorder.record(request(), (delta("normal"),))
    with pytest.raises(RecordingError, match="size limit"):
        save(recorder, tmp_path / "large.json")
    assert not list(tmp_path.iterdir())


def test_audit_reuses_decodes_for_repeated_normalized_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from mux.conformance import recording

    blob = base64.b64encode(b"normalized text only").decode()
    calls = 0
    original = recording.decode_base64

    def count(value: str) -> bytes | None:
        nonlocal calls
        if value == blob:
            calls += 1
        return original(value)

    monkeypatch.setattr(recording, "decode_base64", count)
    value: list[JsonValue] = [delta(blob, index).model_dump(mode="json") for index in range(30)]
    recording.Audit(()).events(value, 0)
    assert calls == 1
