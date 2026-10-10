"""Strict, offline SDK wire replays. A consumed tape is never an outcome verdict.

OpenAI G1 uses Agents session events with Responses-style content, not /responses.
Gemini G2 uses revision-pinned Interactions with authoritative GET snapshots.
The runner owns driver state, host execution, observation and outcome judging.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Literal, NoReturn

import httpx
from daimon.testing.ma_transport import RecordedRequest
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from mux.drivers.gemini.transport import SDKTransport as GeminiTransport
from mux.drivers.openai.transport import SDKTransport as OpenAITransport
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

type Backend = Literal["openai", "gemini"]
type Object = dict[str, JsonValue]
type Query = tuple[tuple[str, str], ...]
_SELECTIONS: dict[Backend, tuple[str, str, str, str, str]] = {
    "openai": ("openai.persistent_workspace", "gpt-6-luna", "agents-sessions", "openai", "2.54.0"),
    "gemini": ("gemini.inline_reuse", "gemini-3.8-flash", "interactions", "google-genai", "2.7.0"),
}
_HEADERS = ("content-type", "accept", "openai-beta", "api-revision", "idempotency-key")


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SourcePin(Record):
    """Hash actual source bytes, not a claimed scenario name or rendered verdict."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def verify(self, root: Path) -> None:
        target = (root / self.path).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ValueError("source pin escapes its root")
        if hashlib.sha256(target.read_bytes()).hexdigest() != self.sha256:
            raise ValueError(f"source pin changed: {self.path}")


class WireFrame(Record):
    payload: Object
    # External virtual-clock/concurrency hooks release these; no wall-clock sleeps.
    after_gate: str | None = None


class WireReply(Record):
    id: str = Field(min_length=1)
    method: Literal["GET", "POST", "DELETE"]
    path: str = Field(pattern=r"^/")
    query: Query = ()
    request_json: JsonValue = None
    headers: Query = ()
    # None means no JSON body, including for GET; matching is always exact.
    status: int = Field(default=200, ge=100, le=599)
    response_json: JsonValue = None
    frames: tuple[WireFrame, ...] | None = None
    after: tuple[str, ...] = ()
    releases: tuple[str, ...] = ()
    # Live recovery streams must stay open until the host closes them.
    hold_open: bool = False

    @model_validator(mode="after")
    def stream_shape(self) -> WireReply:
        if self.frames is not None and self.response_json is not None:
            raise ValueError("reply cannot contain JSON and an SSE stream")
        if self.hold_open and self.frames is None:
            raise ValueError("only an SSE stream can be held open")
        if any(name.lower() not in _HEADERS for name, _ in self.headers):
            raise ValueError("only protocol headers may be matched")
        return self


class ProviderTape(Record):
    version: Literal[1] = 1
    scenario_id: str = Field(min_length=1)
    backend: Backend
    profile: str
    model: str
    api_family: Literal["agents-sessions", "interactions"]
    sdk_distribution: str
    sdk_version: str
    source: SourcePin
    # Native payloads are authored protocol fixtures, not live recordings.
    provenance: Literal["authored-sdk-wire"] = "authored-sdk-wire"
    replies: tuple[WireReply, ...]

    @model_validator(mode="after")
    def closed_contract(self) -> ProviderTape:
        actual = (
            self.profile,
            self.model,
            self.api_family,
            self.sdk_distribution,
            self.sdk_version,
        )
        if actual != _SELECTIONS[self.backend]:
            raise ValueError("tape differs from the explicitly pinned provider contract")
        ids = [reply.id for reply in self.replies]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate wire reply identities")
        known: set[str] = set()
        for reply in self.replies:
            if set(reply.after) - known:
                raise ValueError("reply depends on an unknown or later reply")
            known.add(reply.id)
        return self


class _SSE(httpx.AsyncByteStream):
    def __init__(self, replay: WireReplay, reply: WireReply) -> None:
        self.replay, self.reply = replay, reply
        self.delivered = 0
        self.closed = False
        self._closed_event = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for frame in self.reply.frames or ():
            if frame.after_gate:
                gate = asyncio.create_task(self.replay.gate(frame.after_gate).wait())
                closed = asyncio.create_task(self._closed_event.wait())
                try:
                    await asyncio.wait((gate, closed), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in (gate, closed):
                        task.cancel()
                    await asyncio.gather(gate, closed, return_exceptions=True)
            if self.closed:
                return
            self.delivered += 1
            yield ("data: " + json.dumps(frame.payload) + "\n\n").encode()
        if self.reply.hold_open:
            await self._closed_event.wait()

    async def aclose(self) -> None:
        self.closed = True
        self._closed_event.set()


class WireReplay:
    """Match a dependency-ordered tape without imposing a stream/POST race.

    Each HTTP record is consumed exactly once. Independent stream opens may
    precede accepted mutations; their frames wait on the mutation's explicit gate.
    Unexpected traffic remains a violation even if an SDK wraps the exception.
    """

    def __init__(self, tape: ProviderTape) -> None:
        self.tape = tape
        self.requests: list[RecordedRequest] = []
        self.violations: list[str] = []
        self._consumed: set[str] = set()
        self._gates: dict[str, asyncio.Event] = {}
        self._streams: list[_SSE] = []

    def gate(self, name: str) -> asyncio.Event:
        return self._gates.setdefault(name, asyncio.Event())

    def release(self, name: str) -> None:
        known = {gate for reply in self.tape.replies for gate in reply.releases}
        known.update(
            frame.after_gate
            for reply in self.tape.replies
            for frame in reply.frames or ()
            if frame.after_gate
        )
        if name not in known:
            self._violate("unknown replay gate")
        self.gate(name).set()

    def _violate(self, reason: str) -> NoReturn:
        self.violations.append(reason)
        # Never echo raw bodies, credentials, query values or unexpected headers.
        raise AssertionError(reason)

    def dispatch(self, request: httpx.Request) -> httpx.Response:
        record = RecordedRequest(
            request.method,
            request.url.path,
            tuple(sorted(request.url.params.multi_items())),
            request.read(),
            tuple((name, request.headers[name]) for name in _HEADERS if name in request.headers),
        )
        self.requests.append(record)
        try:
            body: JsonValue = record.json()
        except (ValueError, UnicodeError):
            self._violate("request body is not scripted JSON")
        candidates = [
            reply
            for reply in self.tape.replies
            if reply.id not in self._consumed
            and (reply.method, reply.path) == (record.method, record.path)
            and set(reply.after) <= self._consumed
        ]
        matched = next(
            (
                reply
                for reply in candidates
                if tuple(sorted(reply.query)) == record.query
                and reply.request_json == body
                and all(request.headers.get(name) == value for name, value in reply.headers)
            ),
            None,
        )
        if matched is None:
            self._violate("unscripted, mismatched or premature provider request")
        self._consumed.add(matched.id)
        for name in matched.releases:
            self.release(name)
        if matched.frames is not None:
            source = _SSE(self, matched)
            self._streams.append(source)
            return httpx.Response(
                matched.status, headers={"content-type": "text/event-stream"}, stream=source
            )
        return httpx.Response(matched.status, json=matched.response_json)

    def assert_consumed(self) -> None:
        if self.violations:
            raise AssertionError(f"provider tape has {len(self.violations)} recorded violations")
        remaining = [reply.id for reply in self.tape.replies if reply.id not in self._consumed]
        if remaining:
            raise AssertionError(f"unconsumed provider replies: {remaining}")
        if any(stream.delivered != len(stream.reply.frames or ()) for stream in self._streams):
            raise AssertionError("unconsumed native SSE frames")
        if any(not stream.closed for stream in self._streams):
            raise AssertionError("provider stream not closed")


@dataclass(frozen=True)
class ProviderReplay:
    backend: Backend
    transport: OpenAITransport | GeminiTransport
    wire: WireReplay


@asynccontextmanager
async def provider_replay(
    tape: ProviderTape, *, backend: Backend, profile: str, model: str, source_root: Path
) -> AsyncIterator[ProviderReplay]:
    """Factory selected from the persisted backend; all traffic is MockTransport.

    No environment lookup, key access, SDK retries, router or network fallback.
    The caller injects this transport into its real driver and calls assert_consumed
    only after the host is done; closing a context does not certify an outcome.
    """
    tape = ProviderTape.model_validate(tape.model_dump(mode="json"))
    if (backend, profile, model) != (tape.backend, tape.profile, tape.model):
        raise ValueError("persisted backend/profile/model differs from replay tape")
    tape.source.verify(source_root)
    if version(tape.sdk_distribution) != tape.sdk_version:
        raise ValueError("SDK version differs from the replay source pin")
    wire = WireReplay(tape)
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire.dispatch)) as http:
        if backend == "openai":
            async with AsyncOpenAI(
                api_key="offline-placeholder",
                base_url="https://offline.invalid/v1",
                http_client=http,
                max_retries=0,
            ) as client:
                yield ProviderReplay(backend, OpenAITransport(client), wire)
        else:
            gemini = genai.Client(
                api_key="offline-placeholder",
                http_options=HttpOptions(
                    base_url="https://offline.invalid",
                    httpx_async_client=http,
                    retry_options=HttpRetryOptions(attempts=1),
                ),
            )
            try:
                yield ProviderReplay(backend, GeminiTransport(gemini), wire)
            finally:
                await gemini.aio.aclose()
