"""Neutral record identity survives compatibility display dedup and reconnects."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import base64
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMessageEvent,
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSessionEndTurn,
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsTextBlock,
)
from daimon.core.turn import driver
from daimon.core.turn.io import TurnConnectionLost, TurnEvent, TurnStream
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.run import _DeferredFailureLifecycle
from daimon.core.turn.termination import TerminationReason
from daimon.testing.turn_fakes import FakeAnthropic, RecordingLifecycle
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import Provider
from mux.contracts.receipts import StopObservation

NOW = datetime(2026, 10, 10, tzinfo=UTC)


def record(provider: Provider, revision: int) -> Event:
    return Event(
        id=f"message-{revision}",
        session_id="session",
        sequence=revision,
        turn_id="root",
        type="agent.message",
        observed_at=NOW,
        authority="record",
        payload={
            "item_id": "item",
            "revision": revision,
            "content": [
                {"type": "text", "text": "Actual answer"},
                {
                    "type": "image",
                    "media_type": "image/png",
                    "data_base64": base64.b64encode(f"offline-image-{revision}".encode()).decode(),
                },
            ],
        },
        native=NativeProvenance(provider=provider, api_revision="offline"),
    )


def preview(provider: Provider) -> Event:
    return Event(
        id="preview",
        session_id="session",
        sequence=0,
        turn_id="root",
        type="agent.message.delta",
        observed_at=NOW,
        authority="preview",
        payload={"item_id": "item", "content_index": 0, "text": "draft", "preview_sequence": 0},
        native=NativeProvenance(provider=provider, api_revision="offline"),
    )


class Stream:
    def __init__(self, frames: Sequence[TurnEvent | Exception]) -> None:
        self.frames = iter(frames)
        self.closed = False

    def __aiter__(self) -> Stream:
        return self

    async def __anext__(self) -> TurnEvent:
        try:
            frame = next(self.frames)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(frame, Exception):
            raise frame
        return frame

    async def close(self) -> None:
        self.closed = True


class Codec:
    def __init__(self, scripts: Sequence[Sequence[TurnEvent | Exception]]) -> None:
        self.scripts = iter(scripts)
        self.streams: list[Stream] = []
        self.sent = 0
        self.saved: list[BetaManagedAgentsSessionEvent] = []

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        stream = Stream(next(self.scripts))
        self.streams.append(stream)
        return stream

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        self.sent += 1

    async def replay(self, *, timeout_s: float = 30) -> list[BetaManagedAgentsSessionEvent]:
        return self.saved

    async def status(self) -> str:
        return "running"

    async def interrupt(self, *, timeout_s: float) -> StopObservation | None:
        return None

    async def archive(self) -> None:
        pass


class HookLifecycle(RecordingLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.provider_events: list[Event] = []

    async def on_provider_event(self, event: Event) -> None:
        self.provider_events.append(event)


def native_events() -> tuple[
    BetaManagedAgentsAgentMessageEvent, BetaManagedAgentsSessionStatusIdleEvent
]:
    return (
        BetaManagedAgentsAgentMessageEvent(
            id="same-compatibility-item",
            type="agent.message",
            processed_at=NOW,
            content=[BetaManagedAgentsTextBlock(type="text", text="Actual answer")],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="done",
            type="session.status_idle",
            processed_at=NOW,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    )


async def run(codec: Codec, lifecycle: RecordingLifecycle, *, wrapped: bool) -> None:
    state = await driver.run_turn(
        anthropic=cast(AsyncAnthropic, FakeAnthropic()),
        session_id="session",
        user_message="hello",
        lifecycle=_DeferredFailureLifecycle(lifecycle) if wrapped else lifecycle,
        cancel=asyncio.Event(),
        billing=BillingExempt("headless-unrecorded"),
        render_interval_s=0.001,
        now=lambda: NOW,
    )
    assert state.termination == TerminationReason.COMPLETED
    assert codec.sent == 1 and all(s.closed for s in codec.streams)


@pytest.mark.parametrize("provider", ["openai", "gemini", "anthropic"])
@pytest.mark.parametrize("has_hook", [True, False])
@pytest.mark.parametrize("wrapped", [True, False])
async def test_unique_media_revisions_and_optional_native_frames_have_independent_tap_identity(
    monkeypatch: pytest.MonkeyPatch,
    provider: Provider,
    has_hook: bool,
    wrapped: bool,
) -> None:
    native, done = native_events()
    first, second, delta = record(provider, 1), record(provider, 2), preview(provider)
    codec = Codec(
        [
            [
                TurnEvent(normalized=delta),
                TurnEvent(normalized=delta),
                TurnEvent(native, first),
                TurnEvent(native, first),
                TurnEvent(native, second),
                TurnEvent(normalized=second),
                TurnEvent(done),
            ]
        ]
    )

    def factory(*args: object, **kwargs: object) -> Codec:
        return codec

    monkeypatch.setattr(driver, "turn_io", factory)
    lifecycle = HookLifecycle() if has_hook else RecordingLifecycle()
    await run(codec, lifecycle, wrapped=wrapped)
    assert lifecycle.sse_events == [native, done]
    if isinstance(lifecycle, HookLifecycle):
        assert lifecycle.provider_events == (
            [] if provider == "anthropic" else [delta, first, second]
        )


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("wrapped", [True, False])
async def test_normalized_identity_ledger_survives_reconnect_without_reposting_native_events(
    monkeypatch: pytest.MonkeyPatch,
    provider: Provider,
    wrapped: bool,
) -> None:
    native, done = native_events()
    first, second = record(provider, 1), record(provider, 2)
    codec = Codec(
        [
            [TurnEvent(native, first), TurnConnectionLost("offline reconnect")],
            [TurnEvent(native, first), TurnEvent(native, second), TurnEvent(done)],
        ]
    )
    codec.saved = [native]

    def factory(*args: object, **kwargs: object) -> Codec:
        return codec

    monkeypatch.setattr(driver, "turn_io", factory)
    lifecycle = HookLifecycle()
    await run(codec, lifecycle, wrapped=wrapped)
    assert lifecycle.sse_events == [native, done]
    assert lifecycle.provider_events == [first, second]
    assert lifecycle.reconnects == ["connection_dropped"]


async def test_provider_record_id_equal_to_native_id_does_not_cross_dedup_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, done = native_events()
    normalized = record("openai", 1).model_copy(update={"id": native.id})
    codec = Codec([[TurnEvent(native, normalized), TurnEvent(native, normalized), TurnEvent(done)]])

    def factory(*args: object, **kwargs: object) -> Codec:
        return codec

    monkeypatch.setattr(driver, "turn_io", factory)
    lifecycle = HookLifecycle()
    await run(codec, lifecycle, wrapped=True)
    assert lifecycle.provider_events == [normalized]
    assert lifecycle.sse_events == [native, done]
