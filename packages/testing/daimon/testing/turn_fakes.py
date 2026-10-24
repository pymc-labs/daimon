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
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import httpx
from anthropic import AsyncAnthropic
from anthropic.types import RawMessageStreamEvent
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from daimon.core.config import McpSettings, TurnSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn import run_turn
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason, TurnLifecycle
from daimon.core.turn.state import TurnState
from daimon.testing.turn_leg import MUX_LEG, leg_scope, turn_leg
from mux.testing.drivers.anthropic.ma_models import EMPTY_SESSION_STATS, EMPTY_SESSION_USAGE
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

    stream_scripts: list[list[StreamAction]] = field(default_factory=list[list[StreamAction]])
    replay_events: list[Any] = field(default_factory=list[Any])
    sent_events: list[tuple[str, list[dict[str, Any]]]] = field(
        default_factory=list[tuple[str, list[dict[str, Any]]]]
    )
    stream_calls: int = 0
    stream_timeouts: list[float | httpx.Timeout | anthropic.NotGiven | None] = field(
        default_factory=list[float | httpx.Timeout | anthropic.NotGiven | None]
    )
    streams: list[_FakeEventStream] = field(default_factory=list["_FakeEventStream"])

    async def stream(
        self,
        *,
        session_id: str,
        timeout: float | httpx.Timeout | anthropic.NotGiven | None = None,
        event_deltas: object = None,
    ) -> _FakeEventStream:
        del event_deltas  # the mux codec asks for message previews; the scripts carry full events
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

    def list(
        self,
        *,
        session_id: str,
        types: list[str] | None = None,
        order: str = "asc",
        limit: int | None = None,
    ) -> _FakeEventList:
        # `limit` is the SDK's page size; iterating the list walks every page.
        del limit
        events = [e for e in self.replay_events if types is None or e.type in types]
        if order == "desc":
            events.reverse()
        return _FakeEventList(events)

    async def send(self, session_id: str, *, events: list[dict[str, Any]]) -> SimpleNamespace:
        self.sent_events.append((session_id, list(events)))
        # The SDK returns a `SendEventsResponse`; the mux codec reads `.data` (None: queued).
        return SimpleNamespace(data=None)


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
    #: Every `with_options` call: a single-attempt send asks for max_retries=0.
    options: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    max_retries: int = 8

    def with_options(self, **options: object) -> FakeAnthropic:
        """The same fake (it never retries), with the requested options noted."""
        self.options.append(options)
        return self

    def with_middleware(self, *middleware: object) -> FakeAnthropic:
        """No HTTP is sent by this fake, so redirect middleware has no work."""
        return self


async def run_turn_on_leg(**kwargs: Any) -> TurnState:  # noqa: ANN401 - forwards run_turn's keywords
    """`run_turn` with the selected leg's path and authorized scope, for direct driver tests."""

    kwargs.setdefault("path", turn_leg().path)
    kwargs.setdefault("scope", leg_scope())
    return await run_turn(**kwargs)


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

    renders: list[TurnState] = field(default_factory=list[TurnState])
    terminal_success: list[TurnState] = field(default_factory=list[TurnState])
    terminal_failures: list[tuple[TurnState, Exception]] = field(
        default_factory=list[tuple[TurnState, Exception]]
    )
    sse_events: list[RawMessageStreamEvent] = field(default_factory=list[RawMessageStreamEvent])
    reconnects: list[ReconnectReason] = field(default_factory=list[ReconnectReason])
    rate_limits: list[datetime | None] = field(default_factory=list[datetime | None])
    interrupts: list[InterruptSource] = field(default_factory=list[InterruptSource])

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
    """Static-typing guard: RecordingLifecycle satisfies the Protocol.

    `RecordingLifecycle` subclasses the Protocol explicitly, so pyright
    already checks every method against it; this stays as the callable
    form for tests that want the check spelled out.
    """
    _ = lc  # runtime no-op; compile-time structural check


def deps_for(
    client: object = None,
    sm: async_sessionmaker[AsyncSession] | None = None,
    *,
    leg: TurnSettings | None = None,
) -> TurnDeps:
    """Build the real turn bundle; tests replace only dependencies they exercise.

    The turn path and backend flags come from `leg`, by default the leg the
    run selected through the environment (`daimon.testing.turn_leg`). A test
    that needs one path passes `leg=MUX_LEG` or `leg=LEGACY_LEG`.
    """
    selected = leg if leg is not None else turn_leg()

    return TurnDeps(
        anthropic=cast(AsyncAnthropic, client),
        sessionmaker=sm if sm is not None else async_sessionmaker(),
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=Path("/unused"),
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal(1),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
        tool_safety=OPEN_TOOL_SAFETY,
        turn_path=selected.path,
        channel_backends=selected.channel_backends,
        agent_backends=selected.agent_backends,
    )


def deps_for_pinned_mux(
    client: object = None, sm: async_sessionmaker[AsyncSession] | None = None
) -> TurnDeps:
    """`deps_for` on the mux path with channel backends on, whatever leg the run selected.

    For tests of behaviour that exists only on mux (non-Anthropic backends,
    admission of a configured channel backend). Naming it makes the pin visible;
    tests that should hold on both paths use `deps_for`.
    """
    return deps_for(client, sm, leg=MUX_LEG)
