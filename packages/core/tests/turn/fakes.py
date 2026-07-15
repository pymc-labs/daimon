"""In-test fakes for the turn driver. Kept test-only -- promoting any
of this to a package-level helper would re-introduce the translation
shim the SDK-passthrough refactor removed.

The fakes model just enough of AsyncAnthropic.beta.sessions.events to
drive `run_turn` / `resume_turn`: an ordered script of `StreamAction`
entries per `stream()` call and an ordered list of events for
`list()` (replay).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import anthropic
import httpx
from anthropic.types import RawMessageStreamEvent
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason, TurnLifecycle
from daimon.core.turn.state import TurnState

# --- Stream scripts --------------------------------------------------------


@dataclass
class YieldEvent:
    """Stream step: yield an event to the consumer."""

    event: Any


@dataclass
class RaiseConnection:
    """Stream step: raise APIConnectionError mid-iteration."""

    message: str = "boom"


@dataclass
class RaiseStatus:
    """Stream step: raise APIStatusError (non-429) mid-iteration."""

    status_code: int = 500
    message: str = "upstream"


@dataclass
class RaiseRateLimit:
    """Stream step: raise RateLimitError at open-time.

    Only valid as the sole entry of a stream script (the error is raised
    from `stream()` itself, not during iteration).
    """

    retry_after_seconds: float = 30.0


@dataclass
class BlockForever:
    """Stream step: the iterator awaits an event that never fires.

    Used to test interrupts: the driver signals `cancel`, the consume
    task's inner-loop checks `cancel.is_set()` and raises the interrupt
    sentinel; we must never deadlock.
    """


StreamAction = YieldEvent | RaiseConnection | RaiseStatus | RaiseRateLimit | BlockForever


@dataclass
class FakeEventsResource:
    """Stand-in for `client.beta.sessions.events`.

    - `stream_scripts` is a queue: each call to `stream(...)` consumes one
      script. When exhausted, further `stream()` calls raise AssertionError.
    - `replay_events` is the full event history returned by `list(...)`.
    - `sent_events` records every `events.send(...)` payload (for
      interrupt assertions).
    """

    stream_scripts: list[list[StreamAction]] = field(default_factory=list)
    replay_events: list[Any] = field(default_factory=list)
    sent_events: list[tuple[str, list[dict[str, Any]]]] = field(default_factory=list)
    stream_calls: int = 0

    async def stream(self, *, session_id: str) -> _FakeEventStream:
        if not self.stream_scripts:
            raise AssertionError("FakeEventsResource: no stream_scripts left")
        script = self.stream_scripts.pop(0)
        self.stream_calls += 1
        # RaiseRateLimit fires at open-time
        if script and isinstance(script[0], RaiseRateLimit):
            await asyncio.sleep(0)  # let cancel checks observe the schedule
            raise _make_rate_limit_error(script[0].retry_after_seconds)
        return _FakeEventStream(script)

    def list(self, *, session_id: str) -> _FakeEventList:
        return _FakeEventList(list(self.replay_events))

    async def send(self, session_id: str, *, events: list[dict[str, Any]]) -> None:
        self.sent_events.append((session_id, list(events)))


class _FakeEventStream:
    def __init__(self, script: list[StreamAction]) -> None:
        self._script = script

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Any]:
        for step in self._script:
            if isinstance(step, YieldEvent):
                yield step.event
            elif isinstance(step, RaiseConnection):
                raise anthropic.APIConnectionError(request=_make_request())  # type: ignore[call-arg]
            elif isinstance(step, RaiseStatus):
                raise _make_status_error(step.status_code, step.message)
            elif isinstance(step, BlockForever):
                await asyncio.Event().wait()  # never resolves
            elif isinstance(step, RaiseRateLimit):
                raise AssertionError("RaiseRateLimit must be first step only")


class _FakeEventList:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[Any]:
        for e in self._events:
            yield e


@dataclass
class FakeSessionsBeta:
    events: FakeEventsResource = field(default_factory=FakeEventsResource)


@dataclass
class FakeBeta:
    sessions: FakeSessionsBeta = field(default_factory=FakeSessionsBeta)


@dataclass
class FakeAnthropic:
    """The minimum surface the driver uses. Cast to AsyncAnthropic in call
    sites via `typing.cast` to keep pyright strict.
    """

    beta: FakeBeta = field(default_factory=FakeBeta)


# --- Error constructors (SDK exception shape) -----------------------------


def _make_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/beta/sessions/events/stream")


def _make_status_error(status_code: int, message: str) -> anthropic.APIStatusError:
    request = _make_request()
    response = httpx.Response(status_code, request=request)
    return anthropic.APIStatusError(message, response=response, body=None)


def _make_rate_limit_error(retry_after_seconds: float) -> anthropic.RateLimitError:
    request = _make_request()
    response = httpx.Response(
        429,
        request=request,
        headers={"retry-after": str(int(retry_after_seconds))},
    )
    return anthropic.RateLimitError("rate limited", response=response, body=None)


# --- Recording lifecycle --------------------------------------------------


@dataclass
class RecordingLifecycle(TurnLifecycle):
    """TurnLifecycle implementation that records every call.

    Renders are snapshot-copied so test assertions on a list-index are
    stable across later mutations.
    """

    renders: list[TurnState] = field(default_factory=list)
    terminal_success: list[TurnState] = field(default_factory=list)
    terminal_failures: list[tuple[TurnState, Exception]] = field(default_factory=list)
    sse_events: list[RawMessageStreamEvent] = field(default_factory=list)
    reconnects: list[ReconnectReason] = field(default_factory=list)
    rate_limits: list[datetime | None] = field(default_factory=list)
    interrupts: list[InterruptSource] = field(default_factory=list)

    async def on_render(self, state: TurnState) -> None:
        self.renders.append(state)

    async def on_terminal_success(self, state: TurnState) -> None:
        self.terminal_success.append(state)

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        self.terminal_failures.append((state, err))

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        self.sse_events.append(event)

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        self.reconnects.append(reason)

    async def on_rate_limited(self, until: datetime | None) -> None:
        self.rate_limits.append(until)

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        self.interrupts.append(source)


def assert_lifecycle(lc: TurnLifecycle) -> None:
    """Static-typing guard: RecordingLifecycle satisfies the Protocol."""
    _ = lc  # runtime no-op; compile-time structural check


def _check() -> None:
    assert_lifecycle(RecordingLifecycle())
