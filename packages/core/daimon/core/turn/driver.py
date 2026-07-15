"""Turn driver — pumps an SSE session to terminal idle or error.

Entry point `run_turn` delegates to a module-private `_pump(...)` helper that
runs the consume-loop and render-loop concurrently.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import anthropic as _anthropic
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions import (
    BetaManagedAgentsImageBlockParam,
    BetaManagedAgentsTextBlockParam,
    BetaManagedAgentsUserMessageEventParams,
)
from daimon.core.errors import TurnError
from daimon.core.ma import replay_events, send_interrupt_and_wait, terminal_stop_reason
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TurnState
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt

log = structlog.get_logger(__name__)

InterruptPhase = Literal["pre-stream", "replay", "reattach"]

# Guarded single-render: no-op if `diff(prev, state)` is empty; else calls
# `lifecycle.on_render(state)` and advances the render anchor. Finalizers
# use this (not the raw `lifecycle.on_render`) to honor design §6's
# "exactly one render after the terminal event folds" under the race
# where the render tick already rendered the terminal state.
RenderOnce = Callable[[TurnState], Awaitable[None]]


class _InterruptedDuringRecovery(Exception):
    """User interrupt observed inside `_consume_with_reconnect` before the
    stream is consuming live events (pre-stream, replay, or reattach phase).

    Module-private sentinel; excluded from tenacity's retry predicate so it
    re-raises immediately. Caught exactly once at `_pump`'s top level and
    converted to `TurnError(kind="interrupted")`.
    """

    def __init__(self, *, phase: InterruptPhase) -> None:
        super().__init__(f"interrupted during recovery ({phase})")
        self.phase: InterruptPhase = phase


class _InterruptInConsume(Exception):
    """User interrupt observed while consuming the live SSE stream.

    Module-private sentinel; caught exactly once at `_pump`'s top level.
    """


async def run_turn(
    *,
    anthropic: AsyncAnthropic,
    session_id: str,
    user_message: str,
    lifecycle: TurnLifecycle,
    cancel: asyncio.Event,
    render_interval_s: float = 0.05,
    interrupt_timeout_s: float = 120.0,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    usage_record: Callable[..., Awaitable[None]] | None = None,
    image_blocks: Sequence[BetaManagedAgentsImageBlockParam] | None = None,
) -> TurnState:
    """Open the SSE stream, post the user message, and pump to terminal idle.

    Returns the final `TurnState`.
    """

    async def _send_initial() -> None:
        content: list[BetaManagedAgentsImageBlockParam | BetaManagedAgentsTextBlockParam] = [
            *(image_blocks or []),
            BetaManagedAgentsTextBlockParam(type="text", text=user_message),
        ]
        event: BetaManagedAgentsUserMessageEventParams = {
            "type": "user.message",
            "content": content,
        }
        await anthropic.beta.sessions.events.send(session_id, events=[event])

    return await _pump(
        anthropic=anthropic,
        session_id=session_id,
        send_initial=_send_initial,
        render_anchor=TurnState(),
        seed_state=TurnState(),
        lifecycle=lifecycle,
        cancel=cancel,
        render_interval_s=render_interval_s,
        interrupt_timeout_s=interrupt_timeout_s,
        now=now,
        entry="run",
        usage_record=usage_record,
    )


async def _pump(
    *,
    anthropic: AsyncAnthropic,
    session_id: str,
    send_initial: Callable[[], Awaitable[None]],
    render_anchor: TurnState,
    seed_state: TurnState,
    lifecycle: TurnLifecycle,
    cancel: asyncio.Event,
    render_interval_s: float,
    interrupt_timeout_s: float,
    now: Callable[[], datetime],
    entry: Literal["run", "resume"],
    usage_record: Callable[..., Awaitable[None]] | None,
) -> TurnState:
    log.info("turn.started", session_id=session_id, entry=entry)

    state_cell: list[TurnState] = [seed_state]
    prev_cell: list[TurnState] = [render_anchor]
    events_folded_cell: list[int] = [0]

    from daimon.core.turn.render import diff as _diff  # local import to avoid cycles

    async def _render_once(state: TurnState) -> None:
        delta = _diff(prev_cell[0], state)
        if delta.is_empty():
            return
        await lifecycle.on_render(state)
        prev_cell[0] = state

    async def _render_loop() -> None:
        while True:
            await asyncio.sleep(render_interval_s)
            await _render_once(state_cell[0])

    render_task = asyncio.create_task(_render_loop(), name="turn.render_loop")

    async def _cancel_render() -> None:
        render_task.cancel()
        with _suppress_task_exc():
            await render_task

    # Open + initial-send happen inside the retryable unit on attempt 1 only.
    # On retry, replay + reattach replace them.
    try:
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(2),
                retry=retry_if_exception_type(_anthropic.APIConnectionError),
                reraise=True,
            ):
                with attempt:
                    await _consume_with_reconnect(
                        anthropic=anthropic,
                        session_id=session_id,
                        send_initial=send_initial,
                        is_retry=attempt.retry_state.attempt_number > 1,
                        state_cell=state_cell,
                        events_folded_cell=events_folded_cell,
                        cancel=cancel,
                        lifecycle=lifecycle,
                        usage_record=usage_record,
                    )
        except _InterruptedDuringRecovery as err:
            await _cancel_render()
            return await _finalize_interrupted(
                state_cell=state_cell,
                lifecycle=lifecycle,
                render_once=_render_once,
                session_id=session_id,
                phase=err.phase,
            )
        except _InterruptInConsume:
            await _cancel_render()
            return await _handle_interrupt_in_consume(
                anthropic=anthropic,
                session_id=session_id,
                state_cell=state_cell,
                lifecycle=lifecycle,
                render_once=_render_once,
                interrupt_timeout_s=interrupt_timeout_s,
            )
        except _anthropic.APIConnectionError as err:
            # tenacity exhausted with reraise=True.
            await _cancel_render()
            return await _finalize_connection_lost(
                state_cell=state_cell,
                lifecycle=lifecycle,
                render_once=_render_once,
                session_id=session_id,
                err=err,
            )
        except _anthropic.RateLimitError as err:
            await _cancel_render()
            rate_limit = _compute_rate_limit(err, now)
            return await _finalize_upstream(
                state_cell=state_cell,
                lifecycle=lifecycle,
                render_once=_render_once,
                session_id=session_id,
                err=err,
                rate_limit_until=rate_limit[0] if rate_limit else None,
                retry_after_s=rate_limit[1] if rate_limit else None,
            )
        except _anthropic.APIError as err:
            await _cancel_render()
            return await _finalize_upstream(
                state_cell=state_cell,
                lifecycle=lifecycle,
                render_once=_render_once,
                session_id=session_id,
                err=err,
                rate_limit_until=None,
                retry_after_s=None,
            )

        # Normal termination path.
        await _cancel_render()
        return await _finalize_success_or_error(
            state_cell=state_cell,
            lifecycle=lifecycle,
            render_once=_render_once,
            session_id=session_id,
            events_folded=events_folded_cell[0],
        )
    finally:
        if not render_task.done():
            render_task.cancel()


def _events_since_last_turn_boundary(
    events: list[Any],
) -> list[Any]:
    """Return only the events belonging to the current (most recent) turn.

    In a reused MA session the event log spans multiple turns. Folding the full
    log from TurnState() leaks prior-turn content into the current render state
    (Pitfall 2 of multi-turn reconnect). A turn ends with `session.status_idle`;
    events after the LAST such event belong to the current turn.

    If no `session.status_idle` boundary is found, the whole list is returned
    (single-turn session — no prior-turn content to filter).
    """
    last_boundary = -1
    for i, ev in enumerate(events):
        if getattr(ev, "type", None) == "session.status_idle":
            last_boundary = i
    if last_boundary == -1:
        return events
    return events[last_boundary + 1 :]


async def _consume_with_reconnect(
    *,
    anthropic: AsyncAnthropic,
    session_id: str,
    send_initial: Callable[[], Awaitable[None]],
    is_retry: bool,
    state_cell: list[TurnState],
    events_folded_cell: list[int],
    cancel: asyncio.Event,
    lifecycle: TurnLifecycle,
    usage_record: Callable[..., Awaitable[None]] | None,
) -> None:
    """One attempt at the consume leg. On retry, replay + re-fold first."""
    if cancel.is_set():
        raise _InterruptedDuringRecovery(phase="pre-stream")

    if is_retry:
        log.info("turn.reconnect.started", session_id=session_id)
        await lifecycle.on_reconnect("connection_dropped")
        replayed = await replay_events(anthropic, session_id=session_id)
        if cancel.is_set():
            raise _InterruptedDuringRecovery(phase="replay")
        current_turn_events = _events_since_last_turn_boundary(replayed)
        state_cell[0] = functools.reduce(apply, current_turn_events, TurnState())
        log.info(
            "turn.reconnect.completed",
            session_id=session_id,
            replayed=len(replayed),
        )

    stream = await anthropic.beta.sessions.events.stream(session_id=session_id)
    if cancel.is_set():
        raise _InterruptedDuringRecovery(phase="reattach")

    if not is_retry:
        # Send user.message (or user.tool_confirmation on resume) exactly
        # once, after the first stream open. On retry the server already
        # has these events in its log.
        await send_initial()

    # Race each next-event fetch against `cancel.wait()` so the inner loop
    # is reactive to the interrupt signal even while the stream is idle
    # (no events arriving). `cancel.is_set()` checked pre-loop for the
    # already-set case.
    stream_iter = stream.__aiter__()
    cancel_task = asyncio.create_task(cancel.wait(), name="turn.cancel_waiter")
    try:
        while True:
            if cancel.is_set():
                raise _InterruptInConsume()
            next_coro = cast(Any, stream_iter).__anext__()
            next_task: asyncio.Task[Any] = asyncio.create_task(
                next_coro,
                name="turn.stream_next",
            )
            done, _pending = await asyncio.wait(
                {next_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done:
                next_task.cancel()
                with _suppress_task_exc():
                    await next_task
                raise _InterruptInConsume()
            try:
                event = next_task.result()
            except StopAsyncIteration:
                return
            # Phase 20: per-event metering. Invoke caller-bound usage_record on
            # span.model_request_end events. Exceptions propagate (D-25 fail-closed).
            if usage_record is not None and event.type == "span.model_request_end":
                await usage_record(event=event, session_id=session_id)
            await lifecycle.on_sse_event(event)
            state_cell[0] = apply(state_cell[0], event)
            events_folded_cell[0] += 1
            if event.type == "session.status_terminated":
                return
            if terminal_stop_reason(event) is not None:
                return
    finally:
        if not cancel_task.done():
            cancel_task.cancel()
            with _suppress_task_exc():
                await cancel_task


# --- Finalizers ----------------------------------------------------------


async def _finalize_success_or_error(
    *,
    state_cell: list[TurnState],
    lifecycle: TurnLifecycle,
    render_once: RenderOnce,
    session_id: str,
    events_folded: int,
) -> TurnState:
    final_state = state_cell[0]
    if (
        final_state.error is None
        and final_state.stop_reason is not None
        and final_state.stop_reason.type == "requires_action"
    ):
        # No approval/resume UX is wired for interactive surfaces (Discord/CLI):
        # the driver exits the consume loop on ANY idle, including
        # requires_action, but pre-fix that idle finalized as blank success.
        # Surface it as an actionable failure instead of silently dropping the
        # agent's tool-approval request.
        err = TurnError(
            kind="requires_action",
            message=(
                "The agent requested tool approval — not supported on this "
                "surface yet. Interrupt-free approval/resume UX is a future "
                "feature; routines auto-approve tools."
            ),
        )
        final_state = dataclasses.replace(final_state, error=err)
        state_cell[0] = final_state
    await render_once(final_state)  # guarded final render (§6)
    if final_state.error is not None:
        log.warning(
            "turn.failed",
            session_id=session_id,
            turn_error_kind=final_state.error.kind,
            error=final_state.error.message,
        )
        await lifecycle.on_terminal_failure(final_state, final_state.error)
    else:
        log.info(
            "turn.completed",
            session_id=session_id,
            stop_reason_type=(final_state.stop_reason.type if final_state.stop_reason else None),
            events_folded=events_folded,
        )
        await lifecycle.on_terminal_success(final_state)
    return final_state


async def _finalize_connection_lost(
    *,
    state_cell: list[TurnState],
    lifecycle: TurnLifecycle,
    render_once: RenderOnce,
    session_id: str,
    err: Exception,
) -> TurnState:
    turn_err = TurnError(kind="connection_lost", message=str(err), cause=err)
    state_cell[0] = dataclasses.replace(state_cell[0], error=turn_err, stop_reason=None)
    await render_once(state_cell[0])
    log.warning("turn.reconnect.failed", session_id=session_id, error=str(err))
    log.warning(
        "turn.failed",
        session_id=session_id,
        turn_error_kind="connection_lost",
        error=str(err),
    )
    await lifecycle.on_terminal_failure(state_cell[0], turn_err)
    return state_cell[0]


async def _finalize_upstream(
    *,
    state_cell: list[TurnState],
    lifecycle: TurnLifecycle,
    render_once: RenderOnce,
    session_id: str,
    err: Exception,
    rate_limit_until: datetime | None,
    retry_after_s: float | None,
) -> TurnState:
    turn_err = TurnError(kind="upstream", message=str(err), cause=err)
    state_cell[0] = dataclasses.replace(
        state_cell[0],
        error=turn_err,
        stop_reason=None,  # Clear stale stop_reason -- prevents infinite loops in callers
        rate_limit_until=rate_limit_until or state_cell[0].rate_limit_until,
    )
    await render_once(state_cell[0])
    log.warning(
        "turn.failed",
        session_id=session_id,
        turn_error_kind="upstream",
        error=str(err),
    )
    if rate_limit_until is not None:
        log.warning(
            "turn.rate_limited",
            session_id=session_id,
            retry_after_s=retry_after_s,
            until=rate_limit_until.isoformat(),
        )
        await lifecycle.on_rate_limited(rate_limit_until)
    await lifecycle.on_terminal_failure(state_cell[0], turn_err)
    return state_cell[0]


async def _finalize_interrupted(
    *,
    state_cell: list[TurnState],
    lifecycle: TurnLifecycle,
    render_once: RenderOnce,
    session_id: str,
    phase: InterruptPhase,
) -> TurnState:
    log.info("turn.interrupt.during_reconnect", session_id=session_id, phase=phase)
    turn_err = TurnError(kind="interrupted", message=f"interrupted during {phase}")
    state_cell[0] = dataclasses.replace(state_cell[0], error=turn_err, stop_reason=None)
    await render_once(state_cell[0])
    log.warning(
        "turn.failed",
        session_id=session_id,
        turn_error_kind="interrupted",
        error=turn_err.message,
    )
    await lifecycle.on_terminal_failure(state_cell[0], turn_err)
    return state_cell[0]


async def _handle_interrupt_in_consume(
    *,
    anthropic: AsyncAnthropic,
    session_id: str,
    state_cell: list[TurnState],
    lifecycle: TurnLifecycle,
    render_once: RenderOnce,
    interrupt_timeout_s: float,
) -> TurnState:
    """Normal-flow interrupt: post user.interrupt, wait for terminal idle,
    route to on_terminal_success on ack or on_terminal_failure on timeout.
    """
    try:
        await send_interrupt_and_wait(
            anthropic,
            session_id=session_id,
            timeout_s=interrupt_timeout_s,
        )
    except TurnError as err:
        # send_interrupt_and_wait raises TurnError(kind="interrupt_timeout")
        # on its timeout; propagate through the on_terminal_failure path.
        log.warning(
            "turn.interrupt.timeout",
            session_id=session_id,
            timeout_s=interrupt_timeout_s,
        )
        state_cell[0] = dataclasses.replace(state_cell[0], error=err)
        await render_once(state_cell[0])
        log.warning(
            "turn.failed",
            session_id=session_id,
            turn_error_kind=err.kind,
            error=err.message,
        )
        await lifecycle.on_terminal_failure(state_cell[0], err)
        return state_cell[0]

    log.info("turn.interrupt.sent", session_id=session_id)
    await lifecycle.on_interrupt_sent("cancel_event")
    log.info("turn.interrupt.acked", session_id=session_id)
    # Ack arrived -- partial state is "clean" (refinements §5).
    await render_once(state_cell[0])
    log.info(
        "turn.completed",
        session_id=session_id,
        stop_reason_type=(state_cell[0].stop_reason.type if state_cell[0].stop_reason else None),
        events_folded=None,
    )
    await lifecycle.on_terminal_success(state_cell[0])
    return state_cell[0]


# --- Misc helpers --------------------------------------------------------


@contextlib.contextmanager
def _suppress_task_exc():
    """Swallow any exception (including CancelledError) raised while
    awaiting a cancelled task. Used only to drain the render task.

    Per design §12.6 this is the sole permitted drain point for
    `BaseException` in the driver.
    """
    with contextlib.suppress(BaseException):
        yield


def _compute_rate_limit(
    err: _anthropic.RateLimitError, now: Callable[[], datetime]
) -> tuple[datetime, float] | None:
    """Parse `retry-after` from the 429 response headers.

    SDK note: `RateLimitError` does not expose `retry_after` as an attr.
    The header is the canonical source. Returns `(until, retry_after_s)`
    where `retry_after_s` is the raw header value (avoids clock round-trip
    through `until - now()`); returns None if missing or unparseable.
    """
    response = getattr(err, "response", None)
    if response is None:
        return None
    header = response.headers.get("retry-after")
    if header is None:
        return None
    try:
        retry_after_s = float(header)
    except ValueError:
        return None
    return now() + timedelta(seconds=retry_after_s), retry_after_s
