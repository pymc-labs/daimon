"""Measure real host terminal callbacks; native root authority is a separate join.

This opt-in lifecycle proxy does not complete a turn or decide a deadline.
Invocation return/cleanup time must not replace its terminal callback sample.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from anthropic.types import RawMessageStreamEvent
from daimon.core.turn.lifecycle import (
    Acknowledgment,
    InterruptSource,
    ReconnectReason,
    TurnLifecycle,
)
from daimon.core.turn.state import TurnState
from daimon.testing.host_clock import ClockSample, HostClock, use_host_clock
from mux.contracts.events import Event, TurnEndedPayload
from mux.contracts.ids import ResourceRef


@dataclass(frozen=True)
class TerminalSample:
    observed_s: float
    state: TurnState
    error: Exception | None
    clock_sample: ClockSample | None = None


@dataclass(frozen=True)
class NativeRoot:
    """Fresh native journal joined to an independently persisted send claim."""

    session: ResourceRef
    root_turn_id: str
    journal: Sequence[Event]


@dataclass(frozen=True)
class RootCompletion:
    session_id: str
    root_turn_id: str
    clock_sample: ClockSample


@dataclass(frozen=True)
class ClockedStep[Result]:
    value: Result
    completion: RootCompletion | None


_STEP_COMPLETION: ContextVar[asyncio.Future[RootCompletion] | None] = ContextVar(
    "verified_step_completion", default=None
)


async def await_clocked_step[Result](
    invocation: Awaitable[Result], *, timeout_s: float, clock: HostClock, watchdog_s: float = 10.0
) -> ClockedStep[Result]:
    """Keep the source deadline until actual native proof releases it.

    Generic hooks receive no extra time. Only await_root_completion can publish
    the verified root signal; timestamps alone cannot extend this deadline.
    """
    loop = asyncio.get_running_loop()
    completion: asyncio.Future[RootCompletion] = loop.create_future()
    token = _STEP_COMPLETION.set(completion)
    try:
        with use_host_clock(clock):
            task = asyncio.ensure_future(invocation)
    finally:
        _STEP_COMPLETION.reset(token)
    stop = loop.time() + watchdog_s
    try:
        start = clock.sample()
        for value in (timeout_s, watchdog_s):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("completion budgets must be finite and positive")
        done, _ = await asyncio.wait(
            (task, completion),
            timeout=min(timeout_s, watchdog_s),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise TimeoutError("catalog root completion deadline exceeded")
        if task.done():
            task.result()  # Preserve actual host errors before inspecting any signal.
        proof = completion.result() if completion.done() and not completion.cancelled() else None
        if proof is not None:
            sample = proof.clock_sample
            if sample.clock_id != start.clock_id or sample.order <= start.order:
                raise ValueError("native completion differs from the executor clock window")
            if sample.observed_s - start.observed_s > timeout_s:
                raise TimeoutError("catalog root completion deadline exceeded")
        elif clock() - start.observed_s > timeout_s:
            raise TimeoutError("catalog root completion deadline exceeded")
        value = await asyncio.wait_for(task, timeout=max(0.0, stop - loop.time()))
        return ClockedStep(value, proof)
    finally:
        if not task.done():
            task.cancel()
        if not completion.done():
            completion.cancel()
        await asyncio.gather(task, return_exceptions=True)


class ClockedLifecycle:
    """Forward the actual lifecycle and sample after its terminal callback settles.

    A clock is injected at construction, never derived from expected deadlines.
    The caller retains its delegate for adapter snapshots and captured states.
    A sample alone has no native authority: completion_time requires the fresh
    authoritative journal of the independently acknowledged session/root.
    """

    def __init__(
        self,
        delegate: TurnLifecycle,
        *,
        clock: Callable[[], float] = time.monotonic,
        started_s: float | None = None,
    ) -> None:
        self.delegate = delegate
        self._clock = clock
        self._last = 0.0
        self.started_s = self._read()
        self.started_sample = self._clock.last if isinstance(self._clock, HostClock) else None
        if started_s is not None:
            if (
                isinstance(started_s, bool)
                or not math.isfinite(started_s)
                or not 0 <= started_s <= self.started_s
            ):
                raise ValueError("host start must be an earlier sample in the same clock window")
            self.started_s = started_s
        self._terminal_calls = 0
        self._sample: TerminalSample | None = None
        self._terminal = asyncio.Event()

    def _read(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not math.isfinite(value) or value < self._last:
            raise ValueError("host clock must be finite, nonnegative and monotonic")
        self._last = value
        return value

    @property
    def sample(self) -> TerminalSample | None:
        return self._sample

    @property
    def elapsed_s(self) -> float:
        return self._read() - self.started_s

    def _begin_terminal(self) -> None:
        self._terminal_calls += 1
        if self._terminal_calls != 1:
            raise ValueError("host lifecycle reported multiple terminal callbacks")

    async def on_render(self, state: TurnState) -> None:
        await self.delegate.on_render(state)

    async def on_terminal_success(self, state: TurnState) -> None:
        self._begin_terminal()
        await self.delegate.on_terminal_success(state)
        observed = self._read()
        self._sample = TerminalSample(
            observed, state, None, self._clock.last if isinstance(self._clock, HostClock) else None
        )
        self._terminal.set()

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        self._begin_terminal()
        await self.delegate.on_terminal_failure(state, err)
        observed = self._read()
        self._sample = TerminalSample(
            observed, state, err, self._clock.last if isinstance(self._clock, HostClock) else None
        )
        self._terminal.set()

    async def wait_terminal(self) -> None:
        """Wait for the real delegate to settle; this signal alone proves no root."""
        await self._terminal.wait()

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        await self.delegate.on_sse_event(event)

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        await self.delegate.on_reconnect(reason)

    async def on_rate_limited(self, until: datetime | None) -> None:
        await self.delegate.on_rate_limited(until)

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        await self.delegate.on_interrupt_sent(source)

    async def on_acknowledgment(self, phase: Acknowledgment) -> None:
        hook = getattr(self.delegate, "on_acknowledgment", None)
        if hook is not None:
            await cast(Callable[[Acknowledgment], Awaitable[None]], hook)(phase)

    def completion_time(
        self, *, session: ResourceRef, root_turn_id: str, journal: Sequence[Event]
    ) -> float:
        """Join actual host closure to native authority; never synthesize a terminal.

        The caller must pass a fresh journal slice after the submitted input and
        independently validate its send receipt/binding. A lifecycle return,
        preview, delegated child or unrelated historical root is insufficient.
        """
        sample = self._sample
        if self._terminal_calls != 1 or sample is None or sample.state.termination is None:
            raise ValueError("host has no single settled terminal callback")
        if session.kind != "session" or not root_turn_id:
            raise ValueError("completion needs an independently acknowledged session/root")
        terminals = [event for event in journal if event.type == "session.turn_ended"]
        if not terminals:
            raise ValueError("host callback has no authoritative native terminal")
        if len({event.id for event in terminals}) != len(terminals) or len(
            {event.sequence for event in terminals}
        ) != len(terminals):
            raise ValueError("duplicate journal cannot prove an acknowledged authoritative root")
        for event in terminals:
            payload = TurnEndedPayload.model_validate(event.payload)
            if (
                event.session_id != session.id
                or event.native.provider != session.provider
                or event.turn_id != root_turn_id
                or payload.root_turn_id != root_turn_id
                or event.thread_id is not None
                or event.authority not in ("record", "reconciled")
            ):
                raise ValueError("terminal does not belong to the acknowledged authoritative root")
        return sample.observed_s


async def await_root_completion[Result](
    invocation: Awaitable[Result],
    *,
    lifecycle: ClockedLifecycle,
    timeout_s: float,
    native_root: Callable[[], Awaitable[NativeRoot]],
    watchdog_s: float = 10.0,
) -> Result:
    """Bound actual root completion separately from invocation return/cleanup.

    The source deadline stops only when the real lifecycle has settled AND the
    caller supplies its independently acknowledged fresh authoritative journal.
    Verification and outer return remain bounded by an independent watchdog.
    A refused proof, late/missing root or cancellation drains the invocation.
    """
    task = asyncio.ensure_future(invocation)
    signal = asyncio.create_task(lifecycle.wait_terminal())
    loop = asyncio.get_running_loop()
    stop = loop.time() + watchdog_s
    try:
        for value in (timeout_s, watchdog_s):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("completion budgets must be finite and positive")
        remaining = timeout_s - lifecycle.elapsed_s
        if remaining <= 0:
            raise TimeoutError("catalog root completion deadline exceeded")
        done, _ = await asyncio.wait(
            (task, signal),
            timeout=min(remaining, watchdog_s),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise TimeoutError("catalog root completion deadline exceeded")
        if task.done():
            # Preserve actual host failure; a bare successful return is not proof.
            task.result()
        proof = await asyncio.wait_for(native_root(), timeout=max(0.0, stop - loop.time()))
        completed = lifecycle.completion_time(
            session=proof.session, root_turn_id=proof.root_turn_id, journal=proof.journal
        )
        if completed - lifecycle.started_s > timeout_s:
            raise TimeoutError("catalog root completion deadline exceeded")
        step_signal = _STEP_COMPLETION.get()
        if step_signal is not None:
            sample = lifecycle.sample
            if sample is None or sample.clock_sample is None:
                raise ValueError("verified root has no shared executor clock sample")
            if step_signal.done():
                raise ValueError("host step reported multiple verified roots")
            step_signal.set_result(
                RootCompletion(proof.session.id, proof.root_turn_id, sample.clock_sample)
            )
        return await asyncio.wait_for(task, timeout=max(0.0, stop - loop.time()))
    finally:
        for pending in (task, signal):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, signal, return_exceptions=True)
