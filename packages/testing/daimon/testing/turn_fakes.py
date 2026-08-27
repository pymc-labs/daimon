"""Scripted stand-in for `client.beta.sessions.events` / `client.beta.sessions`,
the surface the turn driver consumes to drive `run_turn` / `resume_turn`: an
ordered script of `StreamAction` entries per `stream()` call and an ordered
list of events for `list()` (replay).

Lives in `daimon.testing` (not a core test tree) because core driver tests,
scheduler convergence tests, and adapter tests all script the same
vocabulary. Deliberately NOT re-exported from `daimon.testing.__init__` —
import from `daimon.testing.turn_fakes` directly.

The fakes sit at the SDK-resource layer (`client.beta.sessions.events`)
rather than the httpx layer: that is the driver's actual dependency surface,
and the SDK does no exception translation during body iteration, so a raw
httpx error raised by a scripted iterator is byte-for-byte what production
raises.
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
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason, TurnLifecycle
from daimon.core.turn.state import TurnState
from daimon.testing.ma import EMPTY_SESSION_STATS, EMPTY_SESSION_USAGE

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
class RaiseStreamDrop:
    """Stream step: raise the raw httpx error a mid-body SSE drop produces.

    The SDK does not wrap this one — it escapes while iterating the already
    open response, not while opening it.
    """

    message: str = (
        "peer closed connection without sending complete message body (incomplete chunked read)"
    )


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


@dataclass
class RaiseReadTimeout:
    """Stream step: raise the raw httpx error a mid-body read stall produces.

    The SDK does NOT wrap this one: body iteration in `anthropic/_streaming.py`
    has no exception translation, so it escapes raw to the driver's
    `__anext__()` — the same shape as `RaiseStreamDrop`.
    """

    message: str = "The read operation timed out"


@dataclass
class DelayThenYield:
    """Stream step: `await asyncio.sleep(seconds)` then yield `event`.

    Used to prove the render loop keeps ticking while the consume loop
    awaits a slow next-event, and to order a cancel against an in-flight
    open/send. Compose with `RaiseReadTimeout` in a script (rather than a
    dedicated `DelayThenRaiseReadTimeout` action) to delay-then-timeout.
    """

    seconds: float
    event: Any


StreamAction = (
    YieldEvent
    | RaiseConnection
    | RaiseStreamDrop
    | RaiseStatus
    | RaiseRateLimit
    | BlockForever
    | RaiseReadTimeout
    | DelayThenYield
)


@dataclass
class FakeEventsResource:
    """Stand-in for `client.beta.sessions.events`.

    - `stream_scripts` is a queue: each call to `stream(...)` consumes one
      script. When exhausted, further `stream()` calls raise AssertionError.
    - `replay_events` is the full event history returned by `list(...)`.
    - `sent_events` records every `events.send(...)` payload (for
      interrupt assertions).
    - `stream_timeouts` records the per-call `timeout` kwarg the driver
      passed to `stream(...)`, in call order (including the `RaiseRateLimit`
      open-time-error path, recorded before the raise).
    - `streams` records every `_FakeEventStream` handed out, so a test can
      assert `streams[0].closed` after the driver abandons a stream it
      opened.
    """

    stream_scripts: list[list[StreamAction]] = field(default_factory=list)
    replay_events: list[Any] = field(default_factory=list)
    sent_events: list[tuple[str, list[dict[str, Any]]]] = field(default_factory=list)
    stream_calls: int = 0
    stream_timeouts: list[Any] = field(default_factory=list[Any])
    streams: list[_FakeEventStream] = field(default_factory=list["_FakeEventStream"])

    async def stream(self, *, session_id: str, timeout: Any = None) -> _FakeEventStream:
        if not self.stream_scripts:
            raise AssertionError("FakeEventsResource: no stream_scripts left")
        script = self.stream_scripts.pop(0)
        self.stream_calls += 1
        self.stream_timeouts.append(timeout)
        # RaiseRateLimit fires at open-time
        if script and isinstance(script[0], RaiseRateLimit):
            await asyncio.sleep(0)  # let cancel checks observe the schedule
            raise _make_rate_limit_error(script[0].retry_after_seconds)
        fake_stream = _FakeEventStream(script)
        self.streams.append(fake_stream)
        return fake_stream

    def list(self, *, session_id: str) -> _FakeEventList:
        return _FakeEventList(list(self.replay_events))

    async def send(self, session_id: str, *, events: list[dict[str, Any]]) -> None:
        self.sent_events.append((session_id, list(events)))


class _FakeEventStream:
    def __init__(self, script: list[StreamAction]) -> None:
        self._script = script
        self.closed: bool = False

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iter()

    async def close(self) -> None:
        """Track whether the driver closed a stream it opened.

        The cancel-coverage plan asserts the driver closes a stream it opened
        but abandoned (e.g. on interrupt or reconnect), rather than leaking
        the underlying connection.
        """
        self.closed = True

    async def _iter(self) -> AsyncIterator[Any]:
        for step in self._script:
            if isinstance(step, YieldEvent):
                yield step.event
            elif isinstance(step, RaiseConnection):
                raise anthropic.APIConnectionError(request=_make_request())  # type: ignore[call-arg]
            elif isinstance(step, RaiseStreamDrop):
                raise httpx.RemoteProtocolError(step.message, request=_make_request())
            elif isinstance(step, RaiseStatus):
                raise _make_status_error(step.status_code, step.message)
            elif isinstance(step, BlockForever):
                await asyncio.Event().wait()  # never resolves
            elif isinstance(step, RaiseRateLimit):
                raise AssertionError("RaiseRateLimit must be first step only")
            elif isinstance(step, RaiseReadTimeout):
                raise httpx.ReadTimeout(step.message, request=_make_request())
            else:
                await asyncio.sleep(step.seconds)
                yield step.event


class _FakeEventList:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[Any]:
        for e in self._events:
            yield e


def _make_session(*, session_id: str, status: str) -> BetaManagedAgentsSession:
    """Build a real `BetaManagedAgentsSession` with fixed, obviously-fake
    values, via the SDK's own validated Pydantic construction -- see
    `guideline:testing`'s validated-construction rule for why unvalidated
    shortcuts are banned here."""
    now = datetime(2026, 1, 1)
    return BetaManagedAgentsSession(
        id=session_id,
        type="session",
        status=status,  # pyright: ignore[reportArgumentType]
        agent=BetaManagedAgentsSessionAgent(
            id="agent_fake",
            type="agent",
            name="fake-agent",
            model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
            mcp_servers=[],
            skills=[],
            tools=[],
            version=1,
        ),
        environment_id="env_fake",
        created_at=now,
        updated_at=now,
        metadata={},
        outcome_evaluations=[],
        resources=[],
        stats=EMPTY_SESSION_STATS,
        usage=EMPTY_SESSION_USAGE,
        vault_ids=[],
    )


@dataclass
class FakeSessionsBeta:
    """Stand-in for `client.beta.sessions`.

    - `retrieve_statuses` is a queue: each call to `retrieve(...)` pops one
      status. Empty queue raises `AssertionError` naming the unexpected
      extra call, rather than defaulting -- a silent default status would
      hide a driver bug where it checks status more often than the test
      intends.
    - `retrieve_calls` records the session ids the driver checked, in order.
    - `retrieve_raises`, when set, makes `retrieve` raise it instead of
      returning -- drives the "status check itself errors" path.
    """

    events: FakeEventsResource = field(default_factory=FakeEventsResource)
    retrieve_statuses: list[str] = field(default_factory=list[str])
    retrieve_calls: list[str] = field(default_factory=list[str])
    retrieve_raises: Exception | None = None

    async def retrieve(self, session_id: str) -> BetaManagedAgentsSession:
        self.retrieve_calls.append(session_id)
        if self.retrieve_raises is not None:
            raise self.retrieve_raises
        if not self.retrieve_statuses:
            raise AssertionError(
                f"FakeSessionsBeta.retrieve: no retrieve_statuses left "
                f"(unexpected extra status check for session_id={session_id!r})"
            )
        status = self.retrieve_statuses.pop(0)
        return _make_session(session_id=session_id, status=status)


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
