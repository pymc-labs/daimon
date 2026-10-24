"""Real SDK proofs that all native response kinds share one durable usage book."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from mux.contracts.ids import ResourceRef
from mux.testing.conformance.budget import BudgetGuard, BudgetRefused, ProbePlan
from mux.testing.drivers.gemini.live_cert import EvidenceAliases
from mux.testing.drivers.gemini.round_cases import MODEL
from mux.testing.drivers.gemini.round_runner import MeterFrames, MeterStream, RoundWire, execute
from mux.testing.drivers.gemini.round_usage import NativeRoute, UsageBook

from .test_round_cases import SCOPE, NativeWire, admission

KINDS = ("create", "cancel-slash", "cancel-colon", "stream", "readback", "terminal", "cleanup")


class AcrossResponses(NativeWire):
    def __init__(self, kind: str, *, malformed: bool = False, drift: str | None = None) -> None:
        super().__init__()
        self.kind, self.malformed, self.drift = kind, malformed, drift
        self.gets = 0

    def snapshot(self, prompt: int) -> dict[str, object]:
        self.stamp += timedelta(seconds=1)
        return {
            "id": "native-owned",
            "status": "cancelled",
            "updated": self.stamp.isoformat(),
            "usage": {
                "total_input_tokens": prompt,
                "total_cached_tokens": False if self.malformed else 0,
                "total_output_tokens": 8,
                "total_thought_tokens": 3,
            },
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        response = super().handle(request)
        is_stream = request.method == "GET" and request.url.params.get("stream") == "true"
        if request.method == "GET" and not is_stream:
            self.gets += 1
        selected = (
            self.kind == "create"
            and request.method == "POST"
            and request.url.path.endswith("/interactions")
            or self.kind.startswith("cancel-")
            and request.method == "POST"
            and not request.url.path.endswith("/interactions")
            or self.kind == "stream"
            and is_stream
            or self.kind == "readback"
            and request.method == "GET"
            and not is_stream
            and self.gets == 1
            or self.kind == "terminal"
            and request.method == "GET"
            and not is_stream
            and self.gets == 2
            or self.kind == "cleanup"
            and request.method == "DELETE"
        )
        if selected:
            if self.kind in ("stream", "cleanup"):
                raw = self.snapshot(64 if self.drift else 2_000_000)
            else:
                raw = response.json()
                if not self.drift:
                    raw["usage"]["total_input_tokens"] = 2_000_000
                    raw["usage"]["total_cached_tokens"] = False if self.malformed else 0
            if self.drift == "root":
                raw["model"] = "gemini-3.5-pro"
            elif self.drift == "agent-config":
                raw["agent_config"] = {"model": "gemini-3.5-pro"}
            elif self.drift == "step":
                raw["steps"] = [{"model": "gemini-3.5-pro"}]
            if self.kind == "stream":
                # Real SDK event shape: usage may accompany ANY event in metadata.
                event = {
                    "event_type": "interaction.status_update",
                    "interaction_id": "native-owned",
                    "event_id": "opaque-event",
                    "status": "cancelled",
                    "updated": raw["updated"],
                    "metadata": {"usage": raw["usage"]},
                }
                if self.drift:
                    event.update(
                        {k: v for k, v in raw.items() if k in ("model", "agent_config", "steps")}
                    )
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=("data: " + json.dumps(event) + "\n\n").encode(),
                )
            if self.kind == "cleanup" and not self.drift:
                raw["interaction"] = self.snapshot(64)  # Later smaller same-owned readback.
            return httpx.Response(200, json=raw)
        if self.kind == "terminal" and request.method == "DELETE":
            return httpx.Response(200, json=self.snapshot(64))
        return response


def pin_cancel_spelling(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    if not kind.startswith("cancel-"):
        return
    original = RoundWire.capture_request

    async def capture(self, request):
        if request.method == "POST" and not request.url.path.endswith("/interactions"):
            suffix = ":cancel" if kind == "cancel-colon" else "/cancel"
            request.url = request.url.copy_with(path="/v1beta/interactions/native-owned" + suffix)
        await original(self, request)

    monkeypatch.setattr(RoundWire, "capture_request", capture)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("malformed", [False, True])
async def test_each_native_response_high_then_smaller_preserves_floor_and_latch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, malformed: bool
) -> None:
    native = AcrossResponses(kind, malformed=malformed)
    pin_cancel_spelling(monkeypatch, kind)
    selected = admission(tmp_path, "C06")
    report = await execute(selected, mock=httpx.MockTransport(native.handle))
    guard = BudgetGuard(selected.config, tmp_path / "spend.md")
    receipt = guard.report()[0]
    assert report["status"] == "fail" and report["actual_usd"] is None
    assert receipt.tokens.input_tokens == 2_000_000
    assert receipt.tokens.output_tokens == 11
    assert receipt.status == "overrun" and receipt.admission_blocked
    assert receipt.held_usd >= receipt.reserved_usd
    assert report["interactions"][0]["overrun"] is True
    with pytest.raises(BudgetRefused):
        guard.reserve(
            ProbePlan(
                provider="gemini", model=MODEL, fixture_id="C06", limits=selected.envelope.limits
            )
        )
    assert len(native.posts) == len(native.cancels) == len(native.deletes) == 1
    assert native.gets == 2  # Canonical stop and sole final GET, both BEFORE DELETE.


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("drift", ["root", "agent-config", "step"])
async def test_native_model_drift_anywhere_refuses_pass_and_keeps_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, drift: str
) -> None:
    native = AcrossResponses(kind, drift=drift)
    pin_cancel_spelling(monkeypatch, kind)
    selected = admission(tmp_path, "C06")
    report = await execute(selected, mock=httpx.MockTransport(native.handle))
    receipt = BudgetGuard(selected.config, tmp_path / "spend.md").report()[0]
    assert report["status"] == "fail" and report["native_model_drift"] is True
    assert report["actual_usd"] is None and receipt.held_usd >= receipt.reserved_usd
    assert len(native.posts) == len(native.deletes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["root", "agent-config", "step"])
async def test_c04_native_get_model_drift_refuses_pass_with_complete_workflow(
    tmp_path: Path, drift: str
) -> None:
    native = NativeWire(initial="completed")

    def handle(request):
        response = native.handle(request)
        if request.method == "GET" and request.url.params.get("stream") != "true":
            raw = response.json()
            if drift == "root":
                raw["model"] = "gemini-3.5-pro"
            elif drift == "agent-config":
                raw["agent_config"] = {"model": "gemini-3.5-pro"}
            else:
                raw["steps"] = [{"model": "gemini-3.5-pro"}]
            return httpx.Response(200, json=raw)
        return response

    selected = admission(tmp_path, "C04")
    report = await execute(selected, mock=httpx.MockTransport(handle))
    receipt = BudgetGuard(selected.config, tmp_path / "spend.md").report()[0]
    assert report["status"] == "fail" and report["native_model_drift"] is True
    assert report["actual_usd"] is None and receipt.held_usd == receipt.reserved_usd
    assert len(native.posts) == len(native.deletes) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cancel-slash", "stream", "cleanup"])
async def test_native_model_drift_without_any_usage_still_refuses_pass(
    tmp_path: Path, kind: str
) -> None:
    native = AcrossResponses(kind, drift="root")

    def handle(request):
        response = native.handle(request)
        selected = (
            kind == "cancel-slash"
            and request.method == "POST"
            and not request.url.path.endswith("/interactions")
            or kind == "stream"
            and request.url.params.get("stream") == "true"
            or kind == "cleanup"
            and request.method == "DELETE"
        )
        if not selected:
            return response
        if kind == "stream":
            event = json.loads(response.content.decode().removeprefix("data: ").strip())
            del event["metadata"]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=("data: " + json.dumps(event) + "\n\n").encode(),
            )
        raw = response.json()
        del raw["usage"]
        return httpx.Response(200, json=raw)

    selected = admission(tmp_path, "C06")
    report = await execute(selected, mock=httpx.MockTransport(handle))
    receipt = BudgetGuard(selected.config, tmp_path / "spend.md").report()[0]
    assert report["status"] == "fail" and report["native_model_drift"] is True
    assert report["actual_usd"] is None and receipt.held_usd == receipt.reserved_usd


@pytest.mark.asyncio
async def test_native_model_drift_stops_further_c04_paid_creates(tmp_path: Path) -> None:
    native = NativeWire(initial="completed")

    def handle(request):
        response = native.handle(request)
        if request.method == "POST" and request.url.path.endswith("/interactions"):
            raw = response.json()
            raw["model"] = "gemini-3.5-pro"
            return httpx.Response(200, json=raw)
        return response

    selected = admission(tmp_path, "C04")
    report = await execute(selected, mock=httpx.MockTransport(handle))
    receipt = BudgetGuard(selected.config, tmp_path / "spend.md").report()[0]
    assert report["status"] == "fail" and report["native_model_drift"] is True
    assert report["actual_usd"] is None and receipt.held_usd == receipt.reserved_usd
    assert len(native.posts) == len(native.deletes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cancel-slash", "cancel-colon", "stream", "cleanup"])
async def test_foreign_response_identity_cannot_bind_large_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    native = AcrossResponses(kind)
    pin_cancel_spelling(monkeypatch, kind)

    def handle(request):
        response = native.handle(request)
        target = (
            kind.startswith("cancel-")
            and request.method == "POST"
            and not request.url.path.endswith("/interactions")
            or kind == "stream"
            and request.url.params.get("stream") == "true"
            or kind == "cleanup"
            and request.method == "DELETE"
        )
        if not target:
            return response
        if kind == "stream":
            raw = json.loads(response.content.decode().removeprefix("data: ").strip())
            raw["interaction_id"] = "foreign"
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=("data: " + json.dumps(raw) + "\n\n").encode(),
            )
        raw = response.json()
        raw["id"] = "foreign"
        return httpx.Response(200, json=raw)

    selected = admission(tmp_path, "C06")
    report = await execute(selected, mock=httpx.MockTransport(handle))
    receipt = BudgetGuard(selected.config, tmp_path / "spend.md").report()[0]
    assert report["status"] == "fail" and report["actual_usd"] is None
    assert receipt.tokens.input_tokens == 64 and not receipt.admission_blocked
    assert receipt.held_usd == receipt.reserved_usd
    assert len(native.posts) == 1


class Chunks(httpx.AsyncByteStream):
    def __init__(self, payload: bytes) -> None:
        self.parts = [payload[:11], payload[11:38], payload[38:]]
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            self.reads += 1
            yield part

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r"])
async def test_fragmented_sse_is_lazy_byte_identical_and_books_metadata_before_decode(
    tmp_path: Path,
    separator: str,
) -> None:
    selected = admission(tmp_path, "C06")
    selected.output.mkdir()
    book = UsageBook(selected.output, EvidenceAliases())
    session = ResourceRef(
        id="local",
        kind="session",
        provider="gemini",
        account_scope_id="qa",
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
    )
    book.receive(
        {"id": "native-owned", "model": MODEL, "updated": datetime.now(UTC).isoformat()},
        NativeRoute("create", session, None, "0" * 64, MODEL),
        success=True,
    )
    route = NativeRoute("stream", session, "native-owned", "0" * 64, MODEL)
    event = {
        "event_type": "interaction.status_update",
        "interaction_id": "native-owned",
        "event_id": "event-42",
        "metadata": {
            "usage": {
                "total_input_tokens": 2_000_000,
                "total_cached_tokens": False,
                "total_output_tokens": 8,
                "total_thought_tokens": 3,
            }
        },
    }
    payload = ("data: " + json.dumps(event) + separator + separator).encode()
    chunks = Chunks(payload)
    tapped = MeterStream(
        chunks,
        MeterFrames(
            lambda raw: book.receive(raw, route, success=True), lambda: book.taint("native-owned")
        ),
    )
    assert chunks.reads == 0
    observed = b"".join([part async for part in tapped])
    assert (
        observed == payload and book.native["native-owned"].maxima["promptTokenCount"] == 2_000_000
    )
    assert book.native["native-owned"].tainted_counters == {"cachedContentTokenCount"}
    assert book.exceeds_interaction_limit()
    await tapped.aclose()
    assert chunks.closed


@pytest.mark.asyncio
async def test_fragmented_stream_still_latches_after_lower_terminal_sdk_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = AcrossResponses("stream", malformed=True)
    chunks: list[Chunks] = []

    def handle(request):
        response = native.handle(request)
        if request.url.params.get("stream") == "true":
            source = Chunks(response.content)
            chunks.append(source)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)
        return response

    original = RoundWire.open_stream

    async def receive_first(self, *args, **kwargs):
        source = await original(self, *args, **kwargs)
        await anext(source)  # Independently received native frame, then controlled client EOF.
        return source

    monkeypatch.setattr(RoundWire, "open_stream", receive_first)
    selected = admission(tmp_path, "C06")
    report = await execute(selected, mock=httpx.MockTransport(handle))
    receipt = BudgetGuard(selected.config, tmp_path / "spend.md").report()[0]
    assert report["status"] == "fail" and report["actual_usd"] is None
    assert receipt.tokens.input_tokens == 2_000_000 and receipt.admission_blocked
    assert len(chunks) == 1 and chunks[0].closed and chunks[0].reads == 3
    assert len(native.posts) == len(native.deletes) == 1 and native.gets == 2


@pytest.mark.asyncio
async def test_all_meter_sources_survive_conflicting_sibling_meter(tmp_path: Path) -> None:
    native = NativeWire()

    def handle(request):
        response = native.handle(request)
        if request.method == "POST" and not request.url.path.endswith("/interactions"):
            raw = response.json()
            raw["usageMetadata"] = {
                "promptTokenCount": 2_000_000,
                "cachedContentTokenCount": False,
                "candidatesTokenCount": 8,
                "thoughtsTokenCount": 3,
            }
            return httpx.Response(200, json=raw)  # usage still reports64.
        return response

    selected = admission(tmp_path, "C06")
    report = await execute(selected, mock=httpx.MockTransport(handle))
    receipt = BudgetGuard(selected.config, tmp_path / "spend.md").report()[0]
    assert report["status"] == "fail" and report["actual_usd"] is None
    assert receipt.tokens.input_tokens == 2_000_000 and receipt.admission_blocked
    assert len(native.posts) == len(native.deletes) == 1


def test_invalid_sse_peer_cannot_hide_later_owned_usage_in_same_chunk(tmp_path: Path) -> None:
    selected = admission(tmp_path, "C06")
    selected.output.mkdir()
    book = UsageBook(selected.output, EvidenceAliases())
    session = ResourceRef(
        id="local",
        kind="session",
        provider="gemini",
        account_scope_id="qa",
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
    )
    book.receive(
        {"id": "native-owned", "updated": datetime.now(UTC).isoformat()},
        NativeRoute("create", session, None, "0" * 64, MODEL),
        success=True,
    )
    route = NativeRoute("stream", session, "native-owned", "0" * 64, MODEL)
    event = {
        "interaction_id": "native-owned",
        "metadata": {"usage": {"total_input_tokens": 2_000_000}},
    }
    payload = b"data: not-json\n\n" + ("data: " + json.dumps(event) + "\n\n").encode()
    frames = MeterFrames(
        lambda raw: book.receive(raw, route, success=True), lambda: book.taint("native-owned")
    )
    with pytest.raises(BudgetRefused, match="frame is malformed"):
        frames.feed(payload)
    assert book.native["native-owned"].maxima["promptTokenCount"] == 2_000_000
    assert book.native["native-owned"].uncertain and book.exceeds_interaction_limit()


def test_created_stream_acceptance_and_idless_metadata_use_sealed_request(tmp_path: Path) -> None:
    start = datetime.now(UTC)
    selected = admission(tmp_path, "C06")
    selected.output.mkdir()
    book = UsageBook(selected.output, EvidenceAliases())
    session = ResourceRef(
        id="local",
        kind="session",
        provider="gemini",
        account_scope_id="qa",
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
    )
    route = NativeRoute("create", session, None, "0" * 64, MODEL)
    book.receive(
        {
            "event_type": "interaction.created",
            "interaction": {"id": "native-owned", "updated": start.isoformat()},
        },
        route,
        success=True,
    )
    book.receive(
        {
            "event_type": "content.delta",
            "metadata": {"usage": {"total_input_tokens": 2_000_000, "total_cached_tokens": False}},
        },
        route,
        success=True,
    )
    book.receive(
        {
            "event_type": "interaction.completed",
            "interaction": {
                "id": "native-owned",
                "updated": (start + timedelta(seconds=1)).isoformat(),
                "usage": {"total_input_tokens": 64},
            },
        },
        route,
        success=True,
    )
    assert (
        len(book.native) == 1
        and book.native["native-owned"].maxima["promptTokenCount"] == 2_000_000
    )
    assert book.exceeds_interaction_limit() and book.native["native-owned"].uncertain
    with pytest.raises(BudgetRefused, match="changed its accepted"):
        book.receive(
            {"event_type": "interaction.created", "interaction": {"id": "foreign"}},
            route,
            success=True,
        )
    assert set(book.native) == {"native-owned"}
