"""Normalized event fakes re-run checks; no HTTP bodies or provider SDKs."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from mux.conformance.budget import TokenUsage
from mux.conformance.fixtures import FIXTURES
from mux.conformance.live_probe import ProbeOutcome, run_probe
from mux.conformance.recording import (
    Recorder,
    RecordingError,
    Replay,
    RequestMetadata,
    replay_fixture,
)
from mux.conformance.reference import ReferenceDriver, ReferenceEvents, Transport
from mux.conformance.runner import Adapter, PendingKind, PendingReason
from mux.conformance.test_budget import plan, setup_guard
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope
from mux.contracts.ports import ManagedAgents


def delta(text: str, sequence: int = 0) -> Event:
    return Event(
        id=f"event-{sequence}",
        session_id="session",
        sequence=sequence,
        type="agent.message.delta",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        authority="preview",
        payload={
            "item_id": "message",
            "content_index": 0,
            "text": text,
            "preview_sequence": sequence,
        },
        native=NativeProvenance(provider="anthropic", api_revision="test"),
    )


def request(path: str = "/sessions/session/events") -> RequestMetadata:
    return RequestMetadata(method="GET", path=path)


def save(recorder: Recorder, path: Path, *, complete: bool = True) -> None:
    recorder.save(path, fixture_id="C16", provider="fake", model="fake", complete=complete)


def test_metadata_projection_never_retains_body_values_query_or_unlisted_headers(
    tmp_path: Path,
) -> None:
    secret = "opaque-secret-must-never-touch-disk"
    meta = RequestMetadata.from_request(
        "post",
        f"https://user:{secret}@fake/events?key={secret}#fragment",
        headers={
            "Authorization": f"Bearer {secret}",
            "X-Goog-Api-Key": secret,
            "content-type": "application/json",
            "accept": "text/event-stream",
            "cookie": secret,
            "custom": secret,
        },
        body={"prompt": secret, "nested": {"response": secret}},
    )
    recorder = Recorder()
    recorder.record(meta, (delta("normalized"),))
    path = tmp_path / "tape.json"
    save(recorder, path)
    stored = path.read_text()
    assert secret not in stored and "https://" not in stored and "fragment" not in stored
    assert meta.path == "/events" and meta.body_fields == ("prompt", "nested")
    assert dict(meta.headers) == {
        "authorization": "[redacted]",
        "x-goog-api-key": "[redacted]",
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(stored)["version"] == 2
    assert "response" not in json.loads(stored)


@pytest.mark.parametrize("field", ["body", "response", "raw", "query"])
def test_metadata_model_has_no_raw_escape_hatch(field: str) -> None:
    with pytest.raises(ValidationError):
        RequestMetadata.model_validate({"method": "GET", "path": "/events", field: "private"})


@pytest.mark.parametrize(
    "headers",
    [
        {"cookie": "private"},
        {"authorization": "Bearer private"},
        {"accept": "private"},
        {"x-goog-api-key": "private"},
    ],
)
def test_direct_metadata_cannot_bypass_allowlist(headers: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        RequestMetadata(method="GET", path="/events", headers=headers)


def test_native_provenance_is_dropped_before_serialization_and_live_event_unchanged(
    tmp_path: Path,
) -> None:
    native = NativeProvenance(
        provider="anthropic",
        api_revision="test",
        record={"raw_response": "secret-native-body"},
        raw_ref="secret-native-ref",
    )
    event = delta("normalized").model_copy(update={"native": native})
    recorder = Recorder()
    recorder.record(request(), (event,))
    path = tmp_path / "event.json"
    save(recorder, path)
    stored = path.read_text()
    assert "secret-native" not in stored and '"record"' not in stored and '"raw_ref"' not in stored
    assert event.native.record == {"raw_response": "secret-native-body"}


async def test_replay_is_ordered_detached_and_has_no_fallback(tmp_path: Path) -> None:
    recorder = Recorder()
    event = Event.model_validate(
        {
            **delta("safe").model_dump(),
            "type": "agent.tool_use",
            "authority": "record",
            "payload": {
                "call_id": "call",
                "tool_name": "tool",
                "input": {"items": ["original"]},
                "executor": "host",
            },
        }
    )
    recorder.record(request("/first"), (event,))
    recorder.record(request("/second"), ())
    input_value = event.payload["input"]
    assert isinstance(input_value, dict)
    items = input_value["items"]
    assert isinstance(items, list)
    items.clear()  # nested JsonValue maps can mutate
    path = tmp_path / "event.json"
    save(recorder, path)
    replay = Replay.load(path)
    with pytest.raises(RecordingError, match="does not match"):
        await replay.events(request("/second"))
    first = await replay.events(request("/first"))
    assert first[0].payload["input"] == {"input_omitted": True}
    replayed_input = first[0].payload["input"]
    assert isinstance(replayed_input, dict)
    replayed_input.clear()
    assert replay.tape.batches[0].events[0].payload["input"] == {"input_omitted": True}
    with pytest.raises(RecordingError, match="unconsumed"):
        replay.finish()
    await replay.events(request("/second"))
    replay.finish()
    with pytest.raises(RecordingError, match="no live fallback"):
        await replay.events(request())


def test_export_never_overwrites_and_cleans_temporary(tmp_path: Path) -> None:
    path = tmp_path / "event.json"
    save(Recorder(), path)
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        save(Recorder(), path)
    assert original == path.read_bytes() and not list(tmp_path.glob("*.tmp"))


def test_invalid_incomplete_and_version_one_raw_tapes_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "tape.json"
    save(Recorder(), path, complete=False)
    with pytest.raises(RecordingError, match="incomplete"):
        Replay.load(path)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "fixture_id": "C16",
                "provider": "fake",
                "model": "fake",
                "complete": True,
                "exchanges": [{"operation": "read", "kind": "bytes", "response": "native-body"}],
            }
        )
    )
    with pytest.raises(RecordingError, match="invalid recording"):
        Replay.load(path)


class RecordedEvents(ReferenceEvents):
    def __init__(self, transport: Transport, tape: Recorder | Replay) -> None:
        super().__init__(transport)
        self._tape = tape

    async def list(self, scope: Scope, session: ResourceRef, *, page: PageRequest) -> Page[Event]:
        self.t.check(scope, session)
        meta = request()
        if isinstance(self._tape, Recorder):
            events: tuple[Event, ...] = ()
            self._tape.record(meta, events)
        else:
            events = await self._tape.events(meta)
        return Page[Event](data=events, has_more=False)


def adapter(tape: Recorder | Replay) -> Adapter:
    transport = Transport()
    driver = ReferenceDriver(transport)
    driver.events = RecordedEvents(transport, tape)
    return Adapter(cast(ManagedAgents, driver), transport.store, transport)


async def test_guarded_normalized_evidence_replays_c16_and_detects_changed_journal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "c16.json"

    async def invoke(recorder: Recorder) -> ProbeOutcome:
        a = adapter(recorder)
        result = await FIXTURES["C16"](a.driver, a.store, a.transport)
        return ProbeOutcome(usage=TokenUsage(input_tokens=0, output_tokens=0), result=result)

    live = await run_probe(setup_guard(tmp_path), plan(), path, invoke)
    assert live.result.status == "pass"
    data = json.loads(path.read_text())
    assert "result" not in data and "verdict" not in data and len(data["batches"]) == 2
    for _ in range(2):
        assert (await replay_fixture(path, adapter)).status == "pass"
    data["batches"][1]["events"] = [delta("changed").model_dump(mode="json")]
    path.write_text(json.dumps(data))
    corrupt = await replay_fixture(path, adapter)
    assert corrupt.status == "fail"
    assert corrupt.evidence == ("check failed: C16: rejected extension changed journal",)


async def test_unconsumed_event_evidence_fails_and_pending_never_certifies(tmp_path: Path) -> None:
    recorder = Recorder()
    for _ in range(3):
        recorder.record(request(), ())
    path = tmp_path / "extra.json"
    save(recorder, path)
    assert (await replay_fixture(path, adapter)).status == "fail"
    reason = PendingReason(PendingKind.LIVE_KEY_REQUIRED, "adapter probe key unavailable")

    def pending(replay: Replay) -> Adapter:
        a = adapter(replay)
        return Adapter(a.driver, a.store, a.transport, pending={"C16": reason})

    result = await replay_fixture(path, pending)
    assert result.status == "pending" and result.pending_reason == reason
