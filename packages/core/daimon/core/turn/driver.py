"""Turn driver — pumps an SSE session to terminal idle or error.

Entry point `run_turn` delegates to a module-private `_pump(...)` helper that
runs the consume-loop and render-loop concurrently. Every call must declare
a `BillingPosture`: `Billed(record=...)` meters each `span.model_request_end`
event through the caller-bound recorder, `BillingExempt(reason=...)` emits a
single structured exempt-billing log line at turn start and meters nothing.

`_pump`'s reconnect machinery is two levels, for two distinct failure modes:

- Outer `while True:` loop — an unbounded, status-gated reconnect for
  eventless cycles (the SSE stream ends or stalls with no terminal event).
  A stream ending or stalling is not itself meaningful: the server closes
  cleanly every ~600s by design, and a healthy long tool call produces
  multiple such cycles. The driver asks MA (`sessions.retrieve`) whether the
  session is still running before deciding anything, so silence alone can
  never finalize a turn as a quiet, truncated success (Class A). This loop
  has no attempt cap by design — the per-turn ceiling is the sole backstop.
- Inner `AsyncRetrying` block — the bounded 2-attempt budget for
  `_CONNECTION_LOST` (a dropped connection, a genuinely different failure
  mode from silence). Each outer iteration gets a fresh `AsyncRetrying`, so
  this budget is per stream generation, not per turn.

What the pump may do inline: the consume loop in `_consume_with_reconnect`
awaits `lifecycle.on_sse_event(event)` synchronously, so per the hook
contract in `turn/lifecycle.py` that hook must stay a cheap local tap — no
network I/O. Exactly ONE piece of I/O is permitted inline in the consume
loop itself, ahead of that hook call: the per-event billing `record()`
call (D-06). Unlike chat-API flush I/O, billing is a local Postgres write,
it is correctness rather than delivery (an unmetered
`span.model_request_end` is revenue lost, and the recorder is fail-closed
by design — exceptions propagate), and it carries no retry-after-style
stall risk. Everything else that wants to talk to the network belongs on
`on_render`, which runs on the separate, never-stalling render task.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar, cast

import anthropic as _anthropic
import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions import (
    BetaManagedAgentsImageBlockParam,
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsStreamSessionEvents,
    BetaManagedAgentsTextBlockParam,
    BetaManagedAgentsUserMessageEventParams,
)
from daimon.core.errors import TurnError
from daimon.core.ma import replay_events, send_interrupt_and_wait, terminal_stop_reason
from daimon.core.turn.approvals import build_confirmation_events, pending_confirmation_ids
from daimon.core.turn.lifecycle import ReconnectReason, TurnLifecycle
from daimon.core.turn.posture import (
    AutoApprove,
    Billed,
    BillingExempt,
    BillingPosture,
    RequireApproval,
    ToolConfirmation,
)
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TurnState
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt

log = structlog.get_logger(__name__)

InterruptPhase = Literal["pre-stream", "stream-open", "send-initial", "replay", "reattach"]

# Module-level singleton so `run_turn`'s default arg isn't a function call in
# the signature (ruff B008) — `RequireApproval()` is a frozen, field-less
# dataclass, so one shared instance is safe across every call.
_DEFAULT_TOOL_CONFIRMATION: ToolConfirmation = RequireApproval()

T = TypeVar("T")

# The SDK only wraps httpx failures raised while *opening* a request into
# `APIConnectionError`. Once an SSE stream is open, a mid-body drop surfaces
# raw from httpx while iterating the response — `RemoteProtocolError` for the
# common "peer closed connection without sending complete message body"
# case. Both mean the same thing to us (stream died, session still alive
# server-side), so both take the reconnect-and-replay path.
#
# `httpx.ReadTimeout` is deliberately NOT in this tuple. A read stall is
# handled as an eventless-cycle signal (see `_EventlessCycle` below), not a
# connection error — the driver asks MA for the session's status before
# deciding anything, so it does not consume the bounded 2-attempt retry
# budget reserved for genuine connection loss.
_CONNECTION_LOST = (_anthropic.APIConnectionError, httpx.RemoteProtocolError)

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


class _EventlessCycle(Exception):
    """The SSE stream ended without a terminal event: either a clean close
    (`StopAsyncIteration`) or a mid-body read stall (`httpx.ReadTimeout`).

    Neither is itself a decision -- a stream can end this way while the
    session is still healthily running (the server's ~600s clean-close
    cadence, or a missed keepalive window). `_pump` asks MA for the
    session's actual status and only then decides to reconnect (status
    still running/rescheduling) or replay-and-finalize (status idle or
    terminated).

    Module-private sentinel; excluded from tenacity's retry predicate (not
    a member of `_CONNECTION_LOST`), so `reraise=True` surfaces it to
    `_pump` immediately rather than consuming the bounded connection-error
    retry budget.
    """

    def __init__(self, *, reason: ReconnectReason) -> None:
        super().__init__(f"eventless cycle ({reason})")
        self.reason: ReconnectReason = reason


async def _await_or_cancel[T](
    coro: Coroutine[Any, Any, T],
    *,
    cancel_task: asyncio.Task[bool],
    phase: InterruptPhase,
    on_cancel_win_result: Callable[[T], Awaitable[None]] | None = None,
) -> T:
    """Race a setup await (`coro`) against `cancel_task` (`FIRST_COMPLETED`).

    Reused at every setup call site (stream-open, send-initial) so a cancel
    signalled while either is in flight is observed promptly instead of
    being silently ignored the way a plain `await` would ignore it — the
    same idiom the consume loop already uses for `stream.__anext__()`.

    On a cancel win: if the work task had NOT yet finished, cancel + drain
    it (`_suppress_task_exc()`). If it HAD already finished with a real
    result despite losing the race — a genuine tie, e.g. the stream opened
    right as cancel fired — `on_cancel_win_result` (when given) is awaited
    with that result so the caller can release it (close the stream) before
    the interrupt propagates; a work task that finished with an exception is
    drained silently, since cancel already wins regardless of what the
    upstream call did. Either way, raises
    `_InterruptedDuringRecovery(phase=phase)`.

    On the normal path (work task wins), returns the work task's result —
    keeps both call sites one line each.
    """
    work_task: asyncio.Task[T] = asyncio.create_task(coro, name=f"turn.{phase.replace('-', '_')}")
    done, _pending = await asyncio.wait(
        {work_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if cancel_task in done:
        if work_task in done:
            if (
                not work_task.cancelled()
                and work_task.exception() is None
                and on_cancel_win_result is not None
            ):
                await on_cancel_win_result(work_task.result())
        else:
            work_task.cancel()
            with _suppress_task_exc():
                await work_task
        raise _InterruptedDuringRecovery(phase=phase)
    return work_task.result()


async def run_turn(
    *,
    anthropic: AsyncAnthropic,
    session_id: str,
    user_message: str,
    lifecycle: TurnLifecycle,
    cancel: asyncio.Event,
    render_interval_s: float = 0.05,
    interrupt_timeout_s: float = 120.0,
    stream_read_timeout_s: float = 120.0,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    billing: BillingPosture,
    tool_confirmation: ToolConfirmation = _DEFAULT_TOOL_CONFIRMATION,
    image_blocks: Sequence[BetaManagedAgentsImageBlockParam] | None = None,
) -> TurnState:
    """Open the SSE stream, post the user message, and pump to terminal idle.

    `stream_read_timeout_s` (default 120.0) is the per-call read timeout
    passed to `events.stream(...)`. A wire probe found the server sending
    `:keepalive` SSE comment frames every 30s on every connection from
    connect; those bytes reset httpx's read clock even though the SDK's SSE
    decoder drops comment lines before the driver ever sees them. 120s is
    four missed keepalives — a genuinely dead socket — and the far more
    common reconnect trigger is expected to be the server's ~600s clean
    close, not this timeout. Both numbers are non-contractual measurements,
    which is why this is an injectable param rather than a constant or an
    env setting.

    Returns the final `TurnState`.
    """
    if isinstance(billing, BillingExempt):
        log.info("turn.billing_exempt", session_id=session_id, reason=billing.reason)

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
        stream_read_timeout_s=stream_read_timeout_s,
        now=now,
        entry="run",
        billing=billing,
        tool_confirmation=tool_confirmation,
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
    stream_read_timeout_s: float,
    now: Callable[[], datetime],
    entry: Literal["run", "resume"],
    billing: BillingPosture,
    tool_confirmation: ToolConfirmation = _DEFAULT_TOOL_CONFIRMATION,
) -> TurnState:
    log.info("turn.started", session_id=session_id, entry=entry)

    state_cell: list[TurnState] = [seed_state]
    prev_cell: list[TurnState] = [render_anchor]
    events_folded_cell: list[int] = [0]
    renders_failed_cell: list[int] = [0]
    # Per-turn dedup for AutoApprove: lives here (not in
    # `_consume_with_reconnect`) so it survives a reconnect -- a
    # re-delivered `requires_action` idle after an eventless-cycle
    # reconnect must not be double-confirmed (T-19-08-B).
    confirmed_tool_use_ids: set[str] = set()

    from daimon.core.turn.render import diff as _diff  # local import to avoid cycles

    async def _render_once(state: TurnState) -> None:
        delta = _diff(prev_cell[0], state)
        if delta.is_empty():
            return
        # Named boundary (guideline:architecture): rendering is delivery,
        # not correctness, and the adapter exceptions it must survive are
        # open-ended (discord.HTTPException, Slack API errors, whatever a
        # future adapter raises). `Exception`, not `BaseException` --
        # CancelledError must still propagate so the render task stays
        # cancellable; `_suppress_task_exc()` remains the sole
        # BaseException drain site in this module.
        #
        # On failure: log and return WITHOUT advancing `prev_cell[0]`. The
        # next tick re-diffs from the unchanged anchor and naturally
        # retries the identical delta -- no retry counter, no backoff, no
        # extra state. This function always returns `None` either way, and
        # every finalizer proceeds to its `on_terminal_*` call regardless,
        # so a render failure can never change the turn's own outcome.
        try:
            await lifecycle.on_render(state)
        except Exception as err:
            renders_failed_cell[0] += 1
            log.warning(
                "turn.render_failed",
                session_id=session_id,
                error_type=type(err).__name__,
                error=str(err),
            )
            return
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

    # Two-level reconnect structure:
    #
    # - Outer `while True:` loop: an unbounded status-gated reconnect for
    #   eventless cycles (`_EventlessCycle` — a clean close or read-timeout
    #   with no terminal event). Unbounded on purpose: a healthy long tool
    #   call produces multiple eventless close/reopen cycles while the
    #   session is genuinely `running`/`rescheduling`, so silence alone is
    #   never treated as suspicion. The per-turn ceiling (a separate,
    #   later change) is the sole backstop on this loop.
    # - Inner `AsyncRetrying` block: the bounded 2-attempt budget for
    #   `_CONNECTION_LOST` (a dropped connection, not silence). Each outer
    #   iteration gets a FRESH `AsyncRetrying`, so the budget is per stream
    #   generation, not per turn.
    #
    # Open + initial-send happen inside the retryable unit on attempt 1 of
    # the FIRST generation only. On any later attempt (a connection-error
    # retry within a generation, or the first attempt of a generation
    # entered after an eventless cycle), replay + reattach replace them.
    try:
        try:
            eventless_reconnect = False
            eventless_reason: ReconnectReason = "connection_dropped"
            while True:
                try:
                    async for attempt in AsyncRetrying(
                        stop=stop_after_attempt(2),
                        retry=retry_if_exception_type(_CONNECTION_LOST),
                        reraise=True,
                    ):
                        with attempt:
                            first_attempt_of_generation = attempt.retry_state.attempt_number == 1
                            if eventless_reconnect and first_attempt_of_generation:
                                attempt_is_retry = True
                                attempt_reason: ReconnectReason = eventless_reason
                            else:
                                attempt_is_retry = not first_attempt_of_generation
                                attempt_reason = "connection_dropped"
                            await _consume_with_reconnect(
                                anthropic=anthropic,
                                session_id=session_id,
                                send_initial=send_initial,
                                is_retry=attempt_is_retry,
                                reconnect_reason=attempt_reason,
                                state_cell=state_cell,
                                events_folded_cell=events_folded_cell,
                                cancel=cancel,
                                lifecycle=lifecycle,
                                billing=billing,
                                tool_confirmation=tool_confirmation,
                                confirmed_tool_use_ids=confirmed_tool_use_ids,
                                stream_read_timeout_s=stream_read_timeout_s,
                            )
                    break  # a terminal event was found — exit the outer loop too
                except _EventlessCycle as cycle:
                    session = await anthropic.beta.sessions.retrieve(session_id)
                    if session.status in {"running", "rescheduling"}:
                        log.info(
                            "turn.eventless_cycle",
                            session_id=session_id,
                            reason=cycle.reason,
                            status=session.status,
                        )
                        eventless_reconnect = True
                        eventless_reason = cycle.reason
                        continue
                    # status in {"idle", "terminated"}: the terminal event
                    # the driver missed lives in the replay history. Fold it
                    # in and finalize — do NOT reopen the stream. UNLESS the
                    # folded state is a `requires_action` idle and
                    # AutoApprove has unconfirmed ids for it: then this
                    # branch sends confirmations and reopens instead (below).
                    log.info(
                        "turn.eventless_cycle_finalizing",
                        session_id=session_id,
                        reason=cycle.reason,
                        status=session.status,
                    )
                    replayed = await replay_events(anthropic, session_id=session_id)
                    current_turn_events = _events_since_last_turn_boundary(replayed)
                    state_cell[0] = functools.reduce(apply, current_turn_events, TurnState())
                    if session.status == "idle":
                        match tool_confirmation:
                            case AutoApprove():
                                fresh = pending_confirmation_ids(
                                    state_cell[0].stop_reason,
                                    confirmed=confirmed_tool_use_ids,
                                )
                                if fresh:
                                    confirmed_tool_use_ids.update(fresh)
                                    decisions = build_confirmation_events(fresh)
                                    # Safe to send here (and only here on this
                                    # branch): `session.status` just came back
                                    # `idle` from the `sessions.retrieve` above,
                                    # i.e. the session is NOT running — the same
                                    # not-running precondition the live loop's
                                    # send documents (a bare `user.*` event sent
                                    # into a RUNNING session returns HTTP 200 and
                                    # is silently ignored). A `terminated`
                                    # session cannot accept events at all, so it
                                    # is deliberately excluded from this branch
                                    # and keeps the unchanged finalize path.
                                    #
                                    # The fresh-ids guard above is what prevents
                                    # a confirm-reconnect-confirm spin: a
                                    # re-delivered `requires_action` for ids
                                    # already in `confirmed_tool_use_ids` yields
                                    # no fresh ids and falls through to `break`.
                                    await anthropic.beta.sessions.events.send(
                                        session_id, events=decisions
                                    )
                                    log.info(
                                        "turn.tool_confirmation.sent",
                                        session_id=session_id,
                                        count=len(fresh),
                                        via="eventless_cycle",
                                    )
                                    eventless_reconnect = True
                                    eventless_reason = cycle.reason
                                    continue
                            case RequireApproval():
                                pass  # fall through -- unchanged interactive behavior
                    break
        except _InterruptedDuringRecovery as err:
            await _cancel_render()
            return await _finalize_interrupted(
                state_cell=state_cell,
                lifecycle=lifecycle,
                render_once=_render_once,
                session_id=session_id,
                phase=err.phase,
                renders_failed=renders_failed_cell[0],
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
                renders_failed=renders_failed_cell[0],
            )
        except _CONNECTION_LOST as err:
            # tenacity exhausted with reraise=True.
            await _cancel_render()
            return await _finalize_connection_lost(
                state_cell=state_cell,
                lifecycle=lifecycle,
                render_once=_render_once,
                session_id=session_id,
                err=err,
                renders_failed=renders_failed_cell[0],
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
                renders_failed=renders_failed_cell[0],
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
                renders_failed=renders_failed_cell[0],
            )

        # Normal termination path.
        await _cancel_render()
        return await _finalize_success_or_error(
            state_cell=state_cell,
            lifecycle=lifecycle,
            render_once=_render_once,
            session_id=session_id,
            events_folded=events_folded_cell[0],
            renders_failed=renders_failed_cell[0],
            tool_confirmation=tool_confirmation,
            confirmed_tool_use_ids=confirmed_tool_use_ids,
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

    Boundary detection deliberately excludes the final event of `events` from
    the scan: this helper is also used on the eventless-cycle finalize path
    (`_pump`, status idle/terminated), where the current turn has ITSELF just
    ended and the last event in `events` is that turn's own terminal
    `session.status_idle`. Counting it as a boundary would strip it (and every
    other current-turn event) from the result -- an empty fold that silently
    drops the very terminal event this replay exists to recover. The mid-turn
    reconnect call site (is_retry) is unaffected: its `events` never end on the
    current turn's own idle (the consume loop returns on a terminal event
    before a reconnect is ever attempted), so the excluded slot was never a
    boundary candidate there either.

    If no `session.status_idle` boundary is found, the whole list is returned
    (single-turn session — no prior-turn content to filter).
    """
    last_boundary = -1
    for i, ev in enumerate(events[:-1]):
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
    reconnect_reason: ReconnectReason,
    state_cell: list[TurnState],
    events_folded_cell: list[int],
    cancel: asyncio.Event,
    lifecycle: TurnLifecycle,
    billing: BillingPosture,
    tool_confirmation: ToolConfirmation,
    confirmed_tool_use_ids: set[str],
    stream_read_timeout_s: float,
) -> None:
    """One attempt at the consume leg. On retry, replay + re-fold first."""
    if cancel.is_set():
        raise _InterruptedDuringRecovery(phase="pre-stream")

    # One waiter task for the whole attempt: races the stream-open and
    # send-initial setup awaits below (via `_await_or_cancel`) as well as
    # every next-event fetch in the consume loop, so a cancel signalled at
    # ANY point after this line is observed promptly rather than only once
    # the consume loop starts. Cancelled + drained in the `finally` below,
    # which now covers the whole attempt (not just the consume loop), so no
    # waiter task leaks on any exit path.
    cancel_task = asyncio.create_task(cancel.wait(), name="turn.cancel_waiter")
    try:
        if is_retry:
            log.info(
                "turn.reconnect.started", session_id=session_id, reconnect_reason=reconnect_reason
            )
            await lifecycle.on_reconnect(reconnect_reason)
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

        async def _open_stream() -> _anthropic.AsyncStream[BetaManagedAgentsStreamSessionEvents]:
            return await anthropic.beta.sessions.events.stream(
                session_id=session_id,
                timeout=httpx.Timeout(stream_read_timeout_s, connect=5.0),
            )

        stream = await _await_or_cancel(
            _open_stream(),
            cancel_task=cancel_task,
            phase="stream-open",
            # A stream that opened anyway on the losing side of the race
            # must not leak the underlying SSE connection.
            on_cancel_win_result=lambda s: s.close(),
        )
        if cancel.is_set():
            raise _InterruptedDuringRecovery(phase="reattach")

        if not is_retry:
            # Send user.message (or user.tool_confirmation on resume) exactly
            # once, after the first stream open. On retry the server already
            # has these events in its log.
            async def _send_initial() -> None:
                await send_initial()

            try:
                await _await_or_cancel(
                    _send_initial(), cancel_task=cancel_task, phase="send-initial"
                )
            except _InterruptedDuringRecovery:
                # The stream (opened above, on the WINNING side of that
                # race) is now abandoned -- close it so this cancel doesn't
                # leak the connection.
                await stream.close()
                raise

        # Race each next-event fetch against the same `cancel_task` so the
        # inner loop is reactive to the interrupt signal even while the
        # stream is idle (no events arriving). `cancel.is_set()` checked
        # pre-loop for the already-set case.
        stream_iter = stream.__aiter__()
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
                # Clean close, no terminal event: not itself completion.
                # Close the abandoned stream and hand the decision to
                # `_pump`'s status check.
                await stream.close()
                raise _EventlessCycle(reason="clean_close") from None
            except httpx.ReadTimeout:
                # No bytes for `stream_read_timeout_s`: same status-checked
                # path as a clean close, not an unhandled crash and not a
                # connection error (it does not consume the bounded
                # `_CONNECTION_LOST` retry budget).
                await stream.close()
                raise _EventlessCycle(reason="read_timeout") from None
            # D-06: the one inline I/O the consume loop is allowed to do.
            # A local Postgres write, correctness not delivery -- unlike the
            # chat-API flush I/O this hook contract forbids, an unmetered
            # event is revenue lost. Exceptions propagate (fail-closed).
            match billing:
                case Billed(record=record):
                    if event.type == "span.model_request_end":
                        await record(event=event)
                case BillingExempt():
                    pass
            await lifecycle.on_sse_event(event)
            state_cell[0] = apply(state_cell[0], event)
            events_folded_cell[0] += 1
            if event.type == "session.status_terminated":
                return
            stop = terminal_stop_reason(event)
            if stop == "requires_action":
                match tool_confirmation:
                    case AutoApprove():
                        assert isinstance(event, BetaManagedAgentsSessionStatusIdleEvent)
                        fresh = pending_confirmation_ids(
                            event.stop_reason, confirmed=confirmed_tool_use_ids
                        )
                        if fresh:
                            confirmed_tool_use_ids.update(fresh)
                            # decisions: one `user.tool_confirmation` event
                            # with `"result": "allow"` per fresh id (built by
                            # approvals.build_confirmation_events, decision 6
                            # -- driver.py only sends).
                            decisions = build_confirmation_events(fresh)
                            # Safe to send here (and ONLY here): this is a
                            # `requires_action` idle, i.e. the session is
                            # NOT running. A bare `user.*` event sent into a
                            # RUNNING session returns HTTP 200 and is
                            # silently ignored (measured 2026-08-26) -- never
                            # move this send to a running-session position.
                            await anthropic.beta.sessions.events.send(session_id, events=decisions)
                            log.info(
                                "turn.tool_confirmation.sent",
                                session_id=session_id,
                                count=len(fresh),
                            )
                            continue
                        # MA is re-asking for ids we already allowed -- stop
                        # instead of spinning until the ceiling (T-19-08-C).
                        log.info("turn.tool_confirmation.exhausted", session_id=session_id)
                        return
                    case RequireApproval():
                        pass  # fall through -- unchanged interactive behavior
            if stop is not None:
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
    renders_failed: int,
    tool_confirmation: ToolConfirmation,
    confirmed_tool_use_ids: set[str],
) -> TurnState:
    final_state = state_cell[0]
    if (
        final_state.error is None
        and final_state.stop_reason is not None
        and final_state.stop_reason.type == "requires_action"
    ):
        # Three distinct paths reach a requires_action idle here:
        # - RequireApproval (interactive, Discord/CLI): no approval/resume
        #   UX is wired -- the consume loop exits on ANY idle, including
        #   requires_action, so this surfaces it as an actionable failure
        #   instead of silently dropping the agent's tool-approval request.
        # - AutoApprove, exhausted: MA re-asked for tool_use_ids already in
        #   `confirmed_tool_use_ids` -- confirmations really were sent, so
        #   the "already confirmed" wording stays.
        # - AutoApprove, never sent: the eventless-cycle idle branch found a
        #   `terminated` session paused on `requires_action` -- it
        #   deliberately does not send into a session that cannot accept
        #   events, so those ids are still unconfirmed here. The "already
        #   confirmed" wording would be a lie in this case; a second wording
        #   names what actually happened.
        match tool_confirmation:
            case AutoApprove():
                unsent = pending_confirmation_ids(
                    final_state.stop_reason, confirmed=confirmed_tool_use_ids
                )
                if unsent:
                    message = (
                        "The agent requested tool approval but the session was "
                        "no longer accepting events, so the turn was abandoned "
                        "without sending confirmations."
                    )
                else:
                    message = (
                        "The agent re-requested approval for tool call(s) already "
                        "confirmed — the confirmation(s) were sent but not accepted, "
                        "so the turn was abandoned rather than spin."
                    )
            case RequireApproval():
                message = (
                    "The agent requested tool approval — not supported on this "
                    "surface yet. Interrupt-free approval/resume UX is a future "
                    "feature; routines auto-approve tools."
                )
        err = TurnError(kind="requires_action", message=message)
        final_state = dataclasses.replace(final_state, error=err)
        state_cell[0] = final_state
    await render_once(final_state)  # guarded final render (§6)
    if final_state.error is not None:
        log.warning(
            "turn.failed",
            session_id=session_id,
            turn_error_kind=final_state.error.kind,
            error=final_state.error.message,
            renders_failed=renders_failed,
        )
        await lifecycle.on_terminal_failure(final_state, final_state.error)
    else:
        log.info(
            "turn.completed",
            session_id=session_id,
            stop_reason_type=(final_state.stop_reason.type if final_state.stop_reason else None),
            events_folded=events_folded,
            renders_failed=renders_failed,
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
    renders_failed: int,
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
        renders_failed=renders_failed,
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
    renders_failed: int,
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
        renders_failed=renders_failed,
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
    renders_failed: int,
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
        renders_failed=renders_failed,
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
    renders_failed: int,
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
            renders_failed=renders_failed,
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
        renders_failed=renders_failed,
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
