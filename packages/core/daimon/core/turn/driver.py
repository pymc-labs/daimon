"""Turn driver — pumps an SSE session to terminal idle or error.

Entry point `run_turn` delegates to a module-private `_pump(...)` helper that
runs the consume-loop and render-loop concurrently. Every call must declare
a `BillingPosture`: `Billed(record=...)` meters each `span.model_request_end`
event through the caller-bound recorder, `BillingExempt(reason=...)` emits a
single structured exempt-billing log line at turn start and meters nothing.

`_pump`'s reconnect machinery is two levels, for two distinct failure modes:

- Outer `while True:` loop — a status-gated reconnect for
  eventless cycles (the SSE stream ends or stalls with no terminal event).
  A stream ending or stalling is not itself meaningful: the server closes
  cleanly every ~600s by design, and a healthy long tool call produces
  multiple such cycles. The driver asks MA (`sessions.retrieve`) whether the
  session is still running before deciding anything, so silence alone can
  never finalize a turn as a quiet, truncated success (Class A). This loop
  has no attempt cap while MA is running. An idle pause with a confirmation
  MA has not acted on yet (no tool result for the call) has two extra
  reconnects before the existing failure. The
  per-turn ceiling
  (`daimon.core.turn.ceiling`), enforced at `bind_session` and
  `run_prepared_turn` for the chat paths and, for callers that bypass those,
  `run_turn`'s own optional `deadline`, is the sole backstop.
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
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar, cast

import anthropic as _anthropic
import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSystemContentBlockParam
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMCPToolResultEvent,
    BetaManagedAgentsAgentToolResultEvent,
    BetaManagedAgentsEventParams,
    BetaManagedAgentsImageBlockParam,
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsSpanModelRequestEndEvent,
    BetaManagedAgentsSystemMessageEventParams,
    BetaManagedAgentsTextBlockParam,
    BetaManagedAgentsUserCustomToolResultEvent,
    BetaManagedAgentsUserMessageEventParams,
    BetaManagedAgentsUserToolConfirmationEventParams,
)
from daimon.core.config import load_turn_settings
from daimon.core.errors import TurnError
from daimon.core.ma import terminal_stop_reason
from daimon.core.tool_safety import ToolCall
from daimon.core.turn.approvals import (
    build_confirmation_events,
    build_decision_events,
    pending_confirmation_ids,
    tool_calls_for,
)
from daimon.core.turn.ceiling import ceiling_error, remaining_s
from daimon.core.turn.degraded import degraded_failure_message
from daimon.core.turn.io import LegacyTurnIO, TurnConnectionLost, TurnIO, TurnStream, turn_io
from daimon.core.turn.lifecycle import ReconnectReason, TurnLifecycle, acknowledge
from daimon.core.turn.outcomes import current_outcome
from daimon.core.turn.posture import (
    AutoApprove,
    Billed,
    BillingExempt,
    BillingPosture,
    PolicyApproval,
    RequireApproval,
    ToolConfirmation,
    ToolConfirmationResult,
)
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import (
    TerminationReason,
    normalized_stop_reason,
    normalized_termination_reason,
    stop_termination_reason,
    termination_reason,
)
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.drivers.anthropic.transport import LegacyTurnTransport
from mux.errors import ProviderError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt

log = structlog.get_logger(__name__)

InterruptPhase = Literal["pre-stream", "stream-open", "send-initial", "replay", "reattach"]

# Module-level singleton so `run_turn`'s default arg isn't a function call in
# the signature (ruff B008) — `RequireApproval()` is a frozen, field-less
# dataclass, so one shared instance is safe across every call.
_DEFAULT_TOOL_CONFIRMATION: ToolConfirmation = RequireApproval()

# An idle MA session can still have an HTTP-accepted confirmation queued.
# Give it two more stream generations to act on it (the call's tool result)
# before treating the quiet pause as a failure; never send the same decision
# again.
_PENDING_CONFIRMATION_RECONNECTS = 2

T = TypeVar("T")


def _answers_in_turn(tool_confirmation: ToolConfirmation) -> bool:
    """Whether this posture answers a `requires_action` idle and keeps going."""
    return isinstance(tool_confirmation, AutoApprove | PolicyApproval)


async def _decide_blocked(
    tool_confirmation: AutoApprove | PolicyApproval,
    state: TurnState,
    fresh: list[str],
    answered: dict[str, ToolConfirmationResult],
) -> list[BetaManagedAgentsUserToolConfirmationEventParams]:
    """The `user.tool_confirmation` batch for `fresh` under an answering posture.

    `PolicyApproval` decides each blocked call independently, then returns
    one event per id.
    """
    match tool_confirmation:
        case AutoApprove():
            return build_confirmation_events(fresh)
        case PolicyApproval(decide=decide):
            calls = tool_calls_for(state, fresh)

            async def decide_one(call: ToolCall) -> ToolConfirmationResult:
                result = await decide(call)
                answered[call.tool_use_id] = result
                return result

            results = await asyncio.gather(*(decide_one(call) for call in calls))
            return build_decision_events(zip(fresh, results, strict=True))


@dataclasses.dataclass(frozen=True)
class _DecisionBatch:
    events: list[BetaManagedAgentsUserToolConfirmationEventParams]
    retire_unsent: tuple[Callable[[], Awaitable[None]], ...]


async def _retire_unsent(
    callbacks: Sequence[Callable[[], Awaitable[None]]], *, session_id: str
) -> None:
    """Make approved cards truthful when their allow never reached MA."""
    results = await asyncio.gather(*(callback() for callback in callbacks), return_exceptions=True)
    for error in results:
        if isinstance(error, BaseException):
            log.warning("turn.confirmation_retire_failed", session_id=session_id, error=str(error))


#: Most time one cleanup step (joining a cancelled card hook, sending the
#: best-effort deny) may take before it is abandoned. Module-level so a test
#: can shorten it.
CLEANUP_BUDGET_S: float = 3.0


async def _bounded(task: asyncio.Task[Any], *, what: str, session_id: str) -> None:
    """Wait for `task` at most `CLEANUP_BUDGET_S`; cancel it after that.

    Never raises and never waits longer than the budget: `asyncio.wait` with a
    timeout returns instead of raising, and a task still running then is
    cancelled and left to unwind on its own — its next await raises
    `CancelledError`, so it does not linger.
    """
    done, _pending = await asyncio.wait({task}, timeout=CLEANUP_BUDGET_S)
    if task in done:
        if not task.cancelled() and task.exception() is not None:
            log.warning(
                "turn.cleanup_failed",
                session_id=session_id,
                step=what,
                error=str(task.exception()),
            )
        return
    task.cancel()
    # Retrieve whatever it ends with, so an abandoned task never logs
    # "exception was never retrieved".
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    log.warning("turn.cleanup_timed_out", session_id=session_id, step=what)


async def _refuse_blocked(io: TurnIO, session_id: str, fresh: list[str], *, message: str) -> None:
    """Send `deny` for every id in `fresh`, best effort.

    The session is on a `requires_action` idle, so the send is accepted and
    the session does not stay paused on calls nobody will answer.
    """
    refusals = build_decision_events(
        (tool_use_id, ToolConfirmationResult(allow=False, deny_message=message))
        for tool_use_id in fresh
    )
    with contextlib.suppress(_anthropic.APIError, ProviderError, TurnConnectionLost):
        await io.send(refusals)


async def _decide_or_refuse_on_cancel(
    tool_confirmation: AutoApprove | PolicyApproval,
    state: TurnState,
    fresh: list[str],
    *,
    cancel: asyncio.Event,
    io: TurnIO,
    session_id: str,
) -> _DecisionBatch:
    """`_decide_blocked`, raced against the turn's cancel signal.

    The one place blocked calls are decided, for the live stream and the
    replay/eventless path alike. A cancel — before the decision, or landing
    in the same tick as it — refuses every fresh id instead and raises
    `_InterruptInConsume` for the normal interrupt path: a stop request never
    lets an `allow` out.

    This function owns the decide task and its cancel waiter. Whatever ends
    the wait — a decision, a cancel, or this coroutine itself being cancelled
    by the turn ceiling or a caller — the `finally` cancels and joins both, so
    a card hook never outlives the turn; the hooks retire their cards on that
    cancellation. An outside cancellation also refuses the pending ids
    (shielded, so the refusal lands even though this task is being torn down)
    before it propagates.
    """
    answered: dict[str, ToolConfirmationResult] = {}
    decide_task = asyncio.create_task(
        _decide_blocked(tool_confirmation, state, fresh, answered), name="turn.decide_blocked"
    )
    cancel_task = asyncio.create_task(cancel.wait(), name="turn.decide_cancel_waiter")
    # Set once the wait ends on its own (a decision or the cancel event); if
    # the `finally` runs with it unset, this coroutine is being torn down from
    # outside (the turn ceiling, a caller) and the pending ids are refused.
    settled = False
    ready = False
    try:
        await asyncio.wait({decide_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        settled = True
        if decide_task.done() and not cancel.is_set():
            events = decide_task.result()
            ready = True
            return _DecisionBatch(
                events,
                tuple(
                    result.retire_unsent
                    for result in answered.values()
                    if result.allow and result.retire_unsent is not None
                ),
            )
    finally:
        # Cleanup is bounded: a card hook retiring its card, or the deny
        # below, talks to a chat API or MA, and an outage there must not hold
        # the turn past its ceiling or a Stop. Each step gets
        # `CLEANUP_BUDGET_S`, then is cancelled and abandoned.
        for task in (decide_task, cancel_task):
            if not task.done():
                task.cancel()
            await _bounded(task, what="decide_task_join", session_id=session_id)
        if not ready:
            await _bounded(
                asyncio.create_task(
                    _retire_unsent(
                        tuple(
                            result.retire_unsent
                            for result in answered.values()
                            if result.allow and result.retire_unsent is not None
                        ),
                        session_id=session_id,
                    ),
                    name="turn.retire_unsent_confirmations",
                ),
                what="retire_unsent_confirmations",
                session_id=session_id,
            )
        if not settled:
            await _bounded(
                asyncio.create_task(
                    _refuse_blocked(
                        io,
                        session_id,
                        fresh,
                        message="This turn ended before the call was approved; it did not run.",
                    ),
                    name="turn.refuse_blocked",
                ),
                what="deadline_refusal",
                session_id=session_id,
            )
    await _bounded(
        asyncio.create_task(
            _refuse_blocked(
                io,
                session_id,
                fresh,
                message="The user stopped this turn; the call did not run.",
            ),
            name="turn.refuse_blocked",
        ),
        what="stop_refusal",
        session_id=session_id,
    )
    raise _InterruptInConsume()


async def _send_decision_batch(
    batch: _DecisionBatch,
    *,
    fresh: list[str],
    cancel: asyncio.Event,
    io: TurnIO,
    session_id: str,
) -> None:
    """Send decisions, retiring approved cards if no allow is sent."""
    try:
        if cancel.is_set():
            raise _InterruptInConsume()
        await io.send(batch.events)
    except BaseException as err:
        await _bounded(
            asyncio.create_task(
                _retire_unsent(batch.retire_unsent, session_id=session_id),
                name="turn.retire_unsent_confirmations",
            ),
            what="retire_unsent_confirmations",
            session_id=session_id,
        )
        if cancel.is_set() or isinstance(err, asyncio.CancelledError | _InterruptInConsume):
            await _bounded(
                asyncio.create_task(
                    _refuse_blocked(
                        io,
                        session_id,
                        fresh,
                        message="This turn ended before the call was approved; it did not run.",
                    ),
                    name="turn.refuse_blocked",
                ),
                what="unsent_refusal",
                session_id=session_id,
            )
        raise


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
_CONNECTION_LOST = (_anthropic.APIConnectionError, httpx.RemoteProtocolError, TurnConnectionLost)

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
    try:
        done, _pending = await asyncio.wait(
            {work_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )
    except BaseException:
        # An OUTER cancellation landed on the race itself -- the ceiling's
        # `asyncio.wait_for` in `run_turn`, or an adapter tearing the turn task
        # down -- so neither branch below ever runs. Without this, `work_task`
        # outlives `run_turn`: nothing cancels it, and when it later completes
        # it hands a freshly opened SSE stream to nobody. The caller's
        # `opened_stream` is only assigned once this function RETURNS, so its
        # `finally` cannot close that stream either; the connection is simply
        # abandoned. Same two cases as the cancel-wins branch below, for the
        # same reasons.
        if not work_task.done():
            work_task.cancel()
            with _suppress_task_exc():
                await work_task
        elif (
            not work_task.cancelled()
            and work_task.exception() is None
            and on_cancel_win_result is not None
        ):
            with _suppress_task_exc():
                await on_cancel_win_result(work_task.result())
        raise
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
    system_blocks: Sequence[BetaManagedAgentsSystemContentBlockParam] = (),
    deadline: datetime | None = None,
    before_send: Callable[[], Awaitable[None]] | None = None,
    send_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    path: Literal["legacy", "mux"] | None = None,
    backend: ManagedAgents | None = None,
    scope: Scope | None = None,
    session_ref: ResourceRef | None = None,
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

    `system_blocks` (default empty) is daimon-authored privileged framing
    for the FIRST send only — the handoff context a replacement session
    needs before its first user message. When non-empty the initial batch is
    `[user.message, system.message]`, in that order: the API accepts at most
    one `system.message` per request, requires it to be the final event, and
    requires it to immediately follow the `user.message` it accompanies. Only
    daimon's own words belong here; quoted material from a previous session
    travels in the user message (see `daimon.core.handoff_context`). Empty is
    byte-identical to the pre-existing single-event send.

    `deadline` (default `None`) is an optional core-owned wall-clock bound
    (`daimon.core.turn.ceiling`). `None` means the driver enforces NOTHING —
    the caller owns the bound — deliberately the OPPOSITE of `bind_session` /
    `run_prepared_turn`, whose `None` is fail-safe (`turn_deadline(now=now())`
    is computed for them). The reason: `run_prepared_turn` already wraps this
    whole call in `asyncio.wait_for`, and its own `TimeoutError` handler is
    what performs D-09's `mark_dead` on the thread-session mapping, logs
    `turn.ceiling_exceeded` with the mapping id, and calls the caller's
    `on_terminal_failure` directly. A driver-level bound that ever won that
    race would silently bypass all of it — so the driver defers to the
    enforcement site that also owns the mapping, and the chat adapters, which
    pass nothing, are byte-identical. The callers that DO pass a deadline are
    the ones that bypass `run_prepared_turn` entirely:
    `daimon.core.headless_runner` (routines, post-deploy smoke) and the CLI's
    `daimon run` — `headless_runner.run_turn` computes its own fail-safe
    `turn_deadline(now)` one layer up and threads it in here.

    On breach: `_pump` is cancelled via `asyncio.wait_for`, so its own
    finalizers never run — this is the single delivery of
    `on_terminal_failure`. No `mark_dead` happens here: a headless routine has
    no `thread_sessions` mapping and the CLI's session is operator-supplied,
    so there is nothing to retire. Honesty note: the one interleaving this
    does not defend against — the timeout landing inside a finalizer's own
    await, producing a second `on_terminal_failure` — is the same one
    `run_prepared_turn` already accepts.

    Returns the final `TurnState`.
    """
    selected_path = path if path is not None else load_turn_settings().path
    io = turn_io(
        anthropic,
        session_id,
        path=selected_path,
        backend=backend,
        scope=scope,
        session_ref=session_ref,
        read_timeout_s=stream_read_timeout_s,
    )
    if isinstance(billing, BillingExempt):
        log.info("turn.billing_exempt", session_id=session_id, reason=billing.reason)

    # A session can be left waiting on tool confirmations no turn will answer:
    # a restart cancelled the turn that owned them before its denials went
    # out (staging, 2026-10-09). MA then refuses every user.message with a
    # 400 and the thread is stuck. The first send detects that once; the
    # turn interrupts the session and starts over. A second refusal is a
    # normal upstream failure.
    unwedge_attempts = [0]
    # Set when recovery failed: the next pump raises the original refusal
    # before opening a stream, so `_pump` finalizes it as an upstream error.
    unwedge_failure: list[BaseException | None] = [None]

    async def _send_initial() -> None:
        content: list[BetaManagedAgentsImageBlockParam | BetaManagedAgentsTextBlockParam] = [
            *(image_blocks or []),
            BetaManagedAgentsTextBlockParam(type="text", text=user_message),
        ]
        event: BetaManagedAgentsUserMessageEventParams = {
            "type": "user.message",
            "content": content,
        }
        batch: list[BetaManagedAgentsEventParams] = [event]
        if system_blocks:
            # LAST, and immediately after the user.message: the live API
            # rejects the whole request otherwise (at most one per request,
            # must be final, must follow a user.message / tool result).
            system_event: BetaManagedAgentsSystemMessageEventParams = {
                "type": "system.message",
                "content": list(system_blocks),
            }
            batch.append(system_event)
        async with send_guard() if send_guard is not None else contextlib.nullcontext():
            if before_send is not None:
                # A last check by the caller, after the stream is open (`run_prepared_turn`).
                await before_send()
            try:
                await io.send(batch)
            except _anthropic.BadRequestError as err:
                if _AWAITING_CONFIRMATIONS in str(err) and unwedge_attempts[0] == 0:
                    unwedge_attempts[0] += 1
                    raise _SessionAwaitingConfirmations() from err
                raise
        await acknowledge(lifecycle, "accepted")

    if (observation := current_outcome.get()) is not None:
        observation.session_id = session_id

    def _new_pump() -> Coroutine[Any, Any, TurnState]:
        return _pump(
            io=io,
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
            preset_error=unwedge_failure[0],
        )

    async def _pump_unwedging() -> TurnState:
        nonlocal io
        try:
            return await _new_pump()
        except _SessionAwaitingConfirmations as stuck:
            log.warning("turn.session_awaiting_confirmations", session_id=session_id)
            if selected_path == "mux":
                if backend is not None:
                    # No SDK authorization for an arbitrary injected backend.
                    # Finalize the original refusal without opening more I/O.
                    unwedge_failure[0] = stuck.__cause__
                    return await _new_pump()
                # Recovery needs the provider's filtered native idle history.
                # Keep main's proof/retry semantics on the existing SDK client;
                # the rejected batch was never accepted. Only this turn falls
                # back, and the closed first stream cannot end the retry.
                io = LegacyTurnIO(anthropic, session_id)
                log.info("turn.confirmation_recovery_legacy", session_id=session_id)
            settled = await _interrupt_and_settle(
                anthropic, session_id=session_id, cancel=cancel, io=io
            )
            if settled == "failed":
                unwedge_failure[0] = stuck.__cause__
            # "cancelled" leaves `cancel` set: the retried pump ends as a Stop.
            # "idle": the retried pump runs the turn normally.
            return await _new_pump()

    pump_coro = _pump_unwedging()
    if deadline is None:
        return await pump_coro

    try:
        return await asyncio.wait_for(pump_coro, timeout=remaining_s(deadline, now=now()))
    except TimeoutError:
        log.error(
            "turn.ceiling_exceeded",
            phase="driver",
            session_id=session_id,
            deadline=deadline.isoformat(),
        )
        err = ceiling_error()
        ceiling_state = TurnState(error=err, termination=TerminationReason.CEILING)
        try:
            await lifecycle.on_terminal_failure(ceiling_state, err)
        except Exception as render_err:
            # Rendering is delivery, not correctness -- a broken adapter hook
            # must not mask the ceiling error itself.
            log.warning("turn.ceiling_render_failed", session_id=session_id, error=str(render_err))
        return ceiling_state


#: MA's refusal of a user.message while confirmations are pending.
_AWAITING_CONFIRMATIONS = "waiting on responses to events"

#: Most a stuck session gets to settle after the interrupt.
_UNWEDGE_SETTLE_S = 15.0


class _SessionAwaitingConfirmations(Exception):
    """MA refused the turn's user.message: the session waits on confirmations."""


async def _interrupt_and_settle(
    anthropic: AsyncAnthropic,
    *,
    session_id: str,
    cancel: asyncio.Event,
    io: TurnIO | None = None,
) -> Literal["idle", "cancelled", "failed"]:
    """Interrupt a session stuck on confirmations and wait until it is idle.

    An interrupt answers the pending calls and ends MA's turn (verified on
    staging, 2026-10-09). No stream is open here, so the interrupt's own
    events cannot end the retried turn early. The whole recovery is bounded
    by `_UNWEDGE_SETTLE_S` and raced against Stop; only a confirmed `idle`
    counts as settled, since MA ignores a message sent while it runs.

    A stuck session is already `idle`, paused on `requires_action`, so the
    status alone settles before MA has taken the interrupt (staging,
    2026-10-09). Settled means idle with a latest pause that no longer
    waits on confirmations.
    """

    recovery_io = io if io is not None else LegacyTurnIO(anthropic, session_id)

    async def _recover() -> bool:
        await recovery_io.send([{"type": "user.interrupt"}])
        while True:
            status = await recovery_io.status()
            if status == "terminated":
                return False
            if status == "idle" and await _latest_idle_is_settled(anthropic, session_id=session_id):
                return True
            await asyncio.sleep(0.5)

    recovery = asyncio.ensure_future(_recover())
    stop = asyncio.ensure_future(cancel.wait())
    try:
        done, _ = await asyncio.wait(
            {recovery, stop}, timeout=_UNWEDGE_SETTLE_S, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for task in (recovery, stop):
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
    if stop in done:
        log.info("turn.session_unwedge_cancelled", session_id=session_id)
        return "cancelled"
    if recovery not in done:
        log.warning("turn.session_unwedge_timeout", session_id=session_id)
        return "failed"
    if (error := recovery.exception()) is not None:
        log.warning("turn.session_unwedge_failed", session_id=session_id, error=str(error))
        return "failed"
    if not recovery.result():
        log.warning("turn.session_unwedge_failed", session_id=session_id, error="terminated")
        return "failed"
    log.info("turn.session_unwedged", session_id=session_id)
    return "idle"


async def _latest_idle_is_settled(anthropic: AsyncAnthropic, *, session_id: str) -> bool:
    """Whether the session's most recent idle exists and is not a `requires_action` pause.

    No idle in the history is no evidence MA took the interrupt, so it does not count.
    """
    return await LegacyTurnTransport(anthropic, session_id).latest_idle_is_settled()


async def _pump(
    *,
    io: TurnIO,
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
    preset_error: BaseException | None = None,
) -> TurnState:
    log.info("turn.started", session_id=session_id, entry=entry)

    state_cell: list[TurnState] = [seed_state]
    prev_cell: list[TurnState] = [render_anchor]
    events_folded_cell: list[int] = [0]
    renders_failed_cell: list[int] = [0]
    normalized_terminal_cell: list[TerminationReason | None] = [None]
    # Per-turn dedup for AutoApprove: lives here (not in
    # `_consume_with_reconnect`) so it survives a reconnect -- a
    # re-delivered `requires_action` idle after an eventless-cycle
    # reconnect must not be double-confirmed (T-19-08-B).
    confirmed_tool_use_ids: set[str] = set()
    # The confirmed ids MA has taken: their `user.tool_confirmation` came
    # back on the stream or in a replay. A `requires_action` idle naming a
    # confirmed id MA has not taken yet is a stale duplicate, not a re-ask
    # (`_consume_with_reconnect`).
    accepted_tool_use_ids: set[str] = set()
    seen_requires_action_event_ids: set[str] = set()
    pending_confirmation_reconnects = 0
    pending_confirmation_retry_ids: frozenset[str] = frozenset()
    delivered_event_ids: set[str] = set()
    # Per-turn billing dedup, shared by the live consume loop and both replay
    # folds: a model call MA emitted while no stream was attached exists only
    # in the replay, and must be billed there, once, by this turn's recorder.
    billed_event_ids: set[str] = set()

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
    # - Outer `while True:` loop: a status-gated reconnect for eventless
    #   cycles (`_EventlessCycle` — a clean close or read-timeout with no
    #   terminal event). It is unbounded while MA is `running`/`rescheduling`:
    #   a healthy long tool call can produce repeated quiet streams. An idle
    #   `requires_action` with sent confirmations MA has not acted on gets at most
    #   two extra stream generations before a requires-action failure. The
    #   per-turn ceiling
    #   (`daimon.core.turn.ceiling`), enforced at `bind_session` and
    #   `run_prepared_turn` for the chat paths and, for callers that bypass
    #   those, `run_turn`'s own optional `deadline`, is the sole backstop
    #   on this loop.
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
            if preset_error is not None:
                # A failed stuck-session recovery: finalize its MA refusal
                # through the handlers below without opening a stream.
                raise preset_error
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
                                io=io,
                                anthropic=anthropic,
                                session_id=session_id,
                                send_initial=send_initial,
                                is_retry=attempt_is_retry,
                                reconnect_reason=attempt_reason,
                                state_cell=state_cell,
                                normalized_terminal_cell=normalized_terminal_cell,
                                events_folded_cell=events_folded_cell,
                                cancel=cancel,
                                lifecycle=lifecycle,
                                billing=billing,
                                tool_confirmation=tool_confirmation,
                                confirmed_tool_use_ids=confirmed_tool_use_ids,
                                accepted_tool_use_ids=accepted_tool_use_ids,
                                seen_requires_action_event_ids=seen_requires_action_event_ids,
                                delivered_event_ids=delivered_event_ids,
                                billed_event_ids=billed_event_ids,
                                stream_read_timeout_s=stream_read_timeout_s,
                            )
                    break  # a terminal event was found — exit the outer loop too
                except _EventlessCycle as cycle:
                    status = await io.status()
                    if status in {"running", "rescheduling"}:
                        log.info(
                            "turn.eventless_cycle",
                            session_id=session_id,
                            reason=cycle.reason,
                            status=status,
                        )
                        eventless_reconnect = True
                        eventless_reason = cycle.reason
                        pending_confirmation_reconnects = 0
                        pending_confirmation_retry_ids = frozenset()
                        continue
                    # For idle or terminated, fold replay before deciding.
                    # A fresh requires-action pause is answered once. An
                    # already-confirmed pause may be stale while MA still has
                    # the batch queued, so give it a bounded reconnect window.
                    log.info(
                        "turn.eventless_cycle_replaying",
                        session_id=session_id,
                        reason=cycle.reason,
                        status=status,
                    )
                    replayed = await io.replay()
                    current_turn_events = _events_since_last_turn_boundary(
                        replayed, tool_confirmation=tool_confirmation
                    )
                    _note_accepted(current_turn_events, accepted_tool_use_ids)
                    _note_requires_action_ids(current_turn_events, seen_requires_action_event_ids)
                    # Replay can add events the stream missed, but it must not
                    # discard events already folded and possibly rendered. A
                    # replay response is fetched through multiple pages and
                    # the SDK does not promise a snapshot; fold its current-turn
                    # suffix onto the monotonic in-memory state instead.
                    await _bill_replayed(billing, current_turn_events, billed_event_ids)
                    state_cell[0] = functools.reduce(apply, current_turn_events, state_cell[0])
                    folded = state_cell[0]
                    if (
                        status == "terminated"
                        and folded.stop_reason is None
                        and folded.error is None
                    ):
                        # MA says terminated, but neither the stream nor the
                        # replay showed this turn ending. Nothing proves it
                        # finished, so it is the same end as a live
                        # `session.status_terminated`, never a success.
                        state_cell[0] = dataclasses.replace(
                            folded,
                            error=TurnError(kind="upstream", message="session terminated by MA"),
                            termination=TerminationReason.SESSION_TERMINATED,
                        )
                    if status == "idle":
                        match tool_confirmation:
                            case AutoApprove() | PolicyApproval():
                                fresh = pending_confirmation_ids(
                                    state_cell[0].stop_reason,
                                    confirmed=confirmed_tool_use_ids,
                                )
                                if fresh:
                                    pending_confirmation_reconnects = 0
                                    pending_confirmation_retry_ids = frozenset()
                                    confirmed_tool_use_ids.update(fresh)
                                    decisions = await _decide_or_refuse_on_cancel(
                                        tool_confirmation,
                                        state_cell[0],
                                        fresh,
                                        cancel=cancel,
                                        io=io,
                                        session_id=session_id,
                                    )
                                    # Safe to send here (and only here on this
                                    # branch): `status` just came back
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
                                    # The fresh-ids guard prevents a
                                    # confirm-reconnect-confirm spin. A later
                                    # duplicate has no fresh ids and enters
                                    # the bounded echo wait below.
                                    await _send_decision_batch(
                                        decisions,
                                        fresh=fresh,
                                        cancel=cancel,
                                        io=io,
                                        session_id=session_id,
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
                                stop_reason = state_cell[0].stop_reason
                                if (
                                    stop_reason is not None
                                    and stop_reason.type == "requires_action"
                                    and stop_reason.event_ids
                                    and not _reasked_after_result(
                                        current_turn_events, set(stop_reason.event_ids)
                                    )
                                ):
                                    retry_ids = frozenset(stop_reason.event_ids)
                                    if retry_ids != pending_confirmation_retry_ids:
                                        pending_confirmation_retry_ids = retry_ids
                                        pending_confirmation_reconnects = 0
                                    if (
                                        pending_confirmation_reconnects
                                        < _PENDING_CONFIRMATION_RECONNECTS
                                    ):
                                        pending_confirmation_reconnects += 1
                                        log.info(
                                            "turn.tool_confirmation.awaiting_echo",
                                            session_id=session_id,
                                            attempt=pending_confirmation_reconnects,
                                            unaccepted=len(
                                                set(stop_reason.event_ids) - accepted_tool_use_ids
                                            ),
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
                io=io,
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
        except (_anthropic.APIError, ProviderError) as err:
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
            normalized_reason=normalized_terminal_cell[0],
            state_cell=state_cell,
            lifecycle=lifecycle,
            render_once=_render_once,
            session_id=session_id,
            events_folded=events_folded_cell[0],
            renders_failed=renders_failed_cell[0],
            tool_confirmation=tool_confirmation,
        )
    finally:
        if not render_task.done():
            # Cancel without draining, unlike `_cancel_render()` on the normal
            # paths. This `finally` also runs while the ceiling's `wait_for` is
            # unwinding a cancellation, and awaiting there is the very hazard
            # `_await_or_cancel`'s own except-branch exists to contain. Safe to
            # leave: `_render_loop` can only exit via CancelledError (its
            # per-tick `_render_once` catches Exception), so nothing goes
            # unretrieved, and the task reaps within a loop turn.
            render_task.cancel()


def _events_since_last_turn_boundary(
    events: list[Any],
    *,
    tool_confirmation: ToolConfirmation,
) -> list[Any]:
    """Return only the events belonging to the current (most recent) turn.

    In a reused MA session the event log spans multiple turns. Folding the full
    log from TurnState() leaks prior-turn content into the current render state
    (Pitfall 2 of multi-turn reconnect). The current turn begins right after the
    last event that ENDED a previous turn.

    The most recent `user.message` anchors the current turn in a complete
    session log. Only idle events before that message can delimit an earlier
    turn; an idle after it belongs to this turn, even when a later
    `session.status_terminated` event follows. The current turn's first terminal
    event also ends the replay suffix, matching the live stream's stop behavior.

    If an incomplete replay omits every user message, retain the legacy
    terminal-idle boundary heuristic. The in-memory state is still folded as
    the base, so already observed current-turn events remain intact.

    Two rules decide which idle events delimit the current turn, and both
    matter:

    1. Under `AutoApprove`, a `session.status_idle` carrying a `requires_action`
       stop reason is NOT a turn boundary. It is a mid-turn PAUSE: the driver
       answers it with `user.tool_confirmation` events and the SAME turn keeps
       going. Treating it as a boundary slices away the pause event itself, so
       the folded `stop_reason` comes back `None`, `pending_confirmation_ids`
       finds nothing to confirm, and a turn genuinely blocked on a tool approval
       is finalized as a quiet success with truncated content.

       This exemption is posture-scoped because the SAME event means the
       opposite thing under `RequireApproval` (the default, and what Discord,
       Slack and `daimon run` use): there the driver has no way to answer, so
       `_finalize_success_or_error` turns that idle into
       `TurnError(kind="requires_action")` and the turn really does END on it.
       Nothing retires the MA session on that error, so the next turn in the
       same thread replays it -- and exempting it there would fold the previous
       turn's content, and its `requires_action` stop reason, into the current
       turn's state. `AutoApprove` cannot hit the mirror-image problem: its one
       production caller (`headless_runner`) opens a fresh session per fire, so
       its replays never span two turns.

    2. An idle after the current `user.message` belongs to this turn, including
       its own terminal idle. This matters when `session.status_terminated`
       follows the idle in history: the idle still ends the turn, and the later
       termination event must not make the idle look like a prior-turn boundary
       or replace the already-complete result. Once the suffix is selected,
       truncate it at the first turn-terminal idle/termination, matching the
       live consume loop. The mid-turn reconnect call site is unaffected by
       terminal truncation because the consume loop returns as soon as it sees
       a terminal event.

    If the replay omits all `user.message` events, exact attribution is
    impossible from the remaining event types alone. In that case, the legacy
    last-idle-before-final-event heuristic is retained; callers fold the result
    onto their existing state so already observed content is not erased.
    """

    def _ends_turn(event: object) -> bool:
        if getattr(event, "type", None) == "session.status_terminated":
            return True
        if not isinstance(event, BetaManagedAgentsSessionStatusIdleEvent):
            return False
        return not (
            _answers_in_turn(tool_confirmation) and event.stop_reason.type == "requires_action"
        )

    current_start = max(
        (i for i, ev in enumerate(events) if getattr(ev, "type", None) == "user.message"),
        default=None,
    )
    boundary_candidates = (
        range(current_start) if current_start is not None else range(len(events) - 1)
    )
    last_boundary = -1
    for i in boundary_candidates:
        ev = events[i]
        if getattr(ev, "type", None) != "session.status_idle":
            continue
        if not _ends_turn(ev):
            continue  # a mid-turn pause, not the end of a turn -- rule 1 above
        last_boundary = i
    current_events = events[last_boundary + 1 :]

    # The live consume loop returns immediately at its first terminal event.
    # A later termination notification is session lifecycle state, not a
    # second outcome for that already-finished turn.
    for i, ev in enumerate(current_events):
        if _ends_turn(ev):
            return current_events[: i + 1]
    return current_events


def _note_accepted(events: Sequence[object], accepted: set[str]) -> None:
    """Add the tool_use ids MA has acted on: those with a tool result.

    A `user.tool_confirmation` in the event history is not proof: the list
    endpoint returns a confirmation as soon as MA has queued it, before MA has
    taken it, and MA takes a batch one per pause (staging, 2026-10-09: six
    queued denies replayed as taken, and the next one-per-pause idle ended
    the turn as a re-ask). MA emits each call's result only after it has
    acted on that call's confirmation, so the result is the evidence.
    """
    accepted.update(
        tool_use_id for event in events if (tool_use_id := _result_tool_use_id(event)) is not None
    )


def _result_tool_use_id(event: object) -> str | None:
    if isinstance(event, BetaManagedAgentsAgentToolResultEvent):
        return event.tool_use_id
    if isinstance(event, BetaManagedAgentsAgentMCPToolResultEvent):
        return event.mcp_tool_use_id
    if isinstance(event, BetaManagedAgentsUserCustomToolResultEvent):
        return event.custom_tool_use_id
    return None


def _note_requires_action_ids(events: Sequence[object], seen: set[str]) -> None:
    """Remember pause event IDs so a replayed old pause cannot become a re-ask."""
    seen.update(
        event.id
        for event in events
        if isinstance(event, BetaManagedAgentsSessionStatusIdleEvent)
        and event.stop_reason.type == "requires_action"
    )


def _reasked_after_result(events: Sequence[object], pending_ids: set[str]) -> bool:
    """Only a replayed pause *after* MA acted on those calls proves a re-ask.

    MA has acted on a call once its tool result is in the history; a queued
    confirmation alone is not enough (`_note_accepted`). The reducer retains
    the last stop reason, so an older pause must not look fresh.
    """
    echoed: set[str] = set()
    for event in events:
        if (tool_use_id := _result_tool_use_id(event)) is not None:
            echoed.add(tool_use_id)
        elif isinstance(event, BetaManagedAgentsSessionStatusIdleEvent):
            reason = event.stop_reason
            if (
                reason.type == "requires_action"
                and set(reason.event_ids) == pending_ids
                and pending_ids <= echoed
            ):
                return True
    return False


async def _bill_once(billing: BillingPosture, event: object, billed_event_ids: set[str]) -> None:
    """Meter one `span.model_request_end` through the turn's recorder, once.

    `billed_event_ids` is per turn and shared across stream generations and
    replays, so a call is billed exactly once whether the driver first sees
    it live, in a replay, or both. The recorder is idempotent in the
    database as well; the set keeps the recorder (and its attribution) from
    running twice. Exceptions propagate (fail-closed).
    """
    if not isinstance(event, BetaManagedAgentsSpanModelRequestEndEvent):
        return
    if event.id in billed_event_ids:
        return
    if (observation := current_outcome.get()) is not None:
        observation.note_usage(event, metered=isinstance(billing, Billed))
    match billing:
        case Billed(record=record):
            await record(event=event)
        case BillingExempt():
            pass
    billed_event_ids.add(event.id)


async def _bill_replayed(
    billing: BillingPosture, events: Sequence[object], billed_event_ids: set[str]
) -> None:
    """Meter the model calls in a replayed current-turn suffix.

    A call MA emitted while no stream was attached (between a dropped or
    closed generation and the next, or after a stall when the session then
    went idle) exists only in the replay. Folding it into the turn state
    without billing it would leave it to the scheduler's usage sweep, which
    records it later under the session's account stamp and a `turn_debit`
    reason instead of this turn's attribution -- or never, where no
    scheduler runs.
    """
    for event in events:
        await _bill_once(billing, event, billed_event_ids)


async def _consume_with_reconnect(
    *,
    normalized_terminal_cell: list[TerminationReason | None],
    io: TurnIO,
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
    accepted_tool_use_ids: set[str],
    seen_requires_action_event_ids: set[str],
    delivered_event_ids: set[str],
    billed_event_ids: set[str],
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
    # Tracks the stream this attempt successfully opened (assigned right
    # after `_await_or_cancel` returns it below), so the `finally` can close
    # it unconditionally regardless of which exit path is taken -- a clean
    # close, a read timeout, a mid-consume cancel, or a `wait_for` ceiling
    # cancellation landing inside the consume loop all leave an open SSE
    # response otherwise. `httpx.Response.aclose()` is idempotent, so this is
    # safe even when an explicit path above already closed it. Does NOT cover
    # the stream-open race in `_await_or_cancel` below: a stream that opens on
    # the losing side of that race never reaches this assignment, and its own
    # `on_cancel_win_result=lambda s: s.close()` already covers it.
    opened_stream: TurnStream | None = None
    # The in-flight next-event fetch, if any. `asyncio.wait` below does not
    # cancel the tasks it waits on when the waiting task itself is cancelled
    # (a ceiling `wait_for` breach, an adapter tearing the turn down), so the
    # `finally` drains it explicitly; otherwise the abandoned `__anext__`
    # lingers as a pending task after the turn has returned.
    next_task: asyncio.Task[Any] | None = None
    try:
        if is_retry:
            log.info(
                "turn.reconnect.started", session_id=session_id, reconnect_reason=reconnect_reason
            )
            await lifecycle.on_reconnect(reconnect_reason)
            replayed = await io.replay()
            if cancel.is_set():
                raise _InterruptedDuringRecovery(phase="replay")
            current_turn_events = _events_since_last_turn_boundary(
                replayed, tool_confirmation=tool_confirmation
            )
            # Preserve events already delivered by the previous stream. The
            # list endpoint is paginated and does not document snapshot
            # completeness, so rebuilding from empty could regress behind the
            # adapter's append-only render anchor if a page omits old history.
            await _bill_replayed(billing, current_turn_events, billed_event_ids)
            _note_accepted(replayed, accepted_tool_use_ids)
            _note_requires_action_ids(current_turn_events, seen_requires_action_event_ids)
            state_cell[0] = functools.reduce(apply, current_turn_events, state_cell[0])
            log.info(
                "turn.reconnect.completed",
                session_id=session_id,
                replayed=len(replayed),
            )

        async def _open_stream() -> TurnStream:
            return await io.open_stream(read_timeout_s=stream_read_timeout_s)

        stream = await _await_or_cancel(
            _open_stream(),
            cancel_task=cancel_task,
            phase="stream-open",
            # A stream that opened anyway on the losing side of the race
            # must not leak the underlying SSE connection.
            on_cancel_win_result=lambda s: s.close(),
        )
        opened_stream = stream
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
                # race) is now abandoned -- the `finally` below closes it
                # unconditionally, so this cancel doesn't leak the connection.
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
            next_task = asyncio.create_task(
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
                item = next_task.result()
                event = item.native
            except StopAsyncIteration:
                # Clean close, no terminal event: not itself completion.
                # The `finally` below closes the abandoned stream; hand the
                # decision to `_pump`'s status check.
                raise _EventlessCycle(reason="clean_close") from None
            except httpx.ReadTimeout:
                # No bytes for `stream_read_timeout_s`: same status-checked
                # path as a clean close, not an unhandled crash and not a
                # connection error (it does not consume the bounded
                # `_CONNECTION_LOST` retry budget). The `finally` below
                # closes the abandoned stream.
                raise _EventlessCycle(reason="read_timeout") from None
            if event.id not in delivered_event_ids:
                # D-06: the one inline I/O the consume loop is allowed to do.
                # A local Postgres write, correctness not delivery -- unlike the
                # chat-API flush I/O this hook contract forbids, an unmetered
                # event is revenue lost. Exceptions propagate (fail-closed).
                await _bill_once(billing, event, billed_event_ids)
                await lifecycle.on_sse_event(event)
                delivered_event_ids.add(event.id)
            _note_accepted((event,), accepted_tool_use_ids)
            state_cell[0] = apply(state_cell[0], event, usage=item.usage)
            events_folded_cell[0] += 1
            normalized = item.normalized
            if normalized is not None:
                reason = normalized_termination_reason(normalized)
                if reason is not None:
                    normalized_terminal_cell[0] = reason
            if event.type == "session.status_terminated":
                return
            # Unknown root idle reasons stay opaque in normalization. The
            # temporary SDK edge preserves legacy's terminal handling of them.
            stop = (
                normalized_stop_reason(normalized)
                if normalized is not None and normalized.type != "native.session.status_idle"
                else terminal_stop_reason(event)
            )
            if stop == "requires_action":
                seen_before = event.id in seen_requires_action_event_ids
                seen_requires_action_event_ids.add(event.id)
                match tool_confirmation:
                    case AutoApprove() | PolicyApproval():
                        assert isinstance(event, BetaManagedAgentsSessionStatusIdleEvent)
                        fresh = pending_confirmation_ids(
                            event.stop_reason, confirmed=confirmed_tool_use_ids
                        )
                        if fresh:
                            confirmed_tool_use_ids.update(fresh)
                            # decisions: one `user.tool_confirmation` event per
                            # fresh id -- `allow` for AutoApprove, the
                            # decider's own answer for PolicyApproval (built
                            # in approvals, decision 6 -- driver.py only
                            # sends). A decider may wait on a person, so the
                            # wait races the cancel signal; a cancel refuses
                            # every pending call before the interrupt, so the
                            # session is not left paused on them.
                            decisions = await _decide_or_refuse_on_cancel(
                                tool_confirmation,
                                state_cell[0],
                                fresh,
                                cancel=cancel,
                                io=io,
                                session_id=session_id,
                            )
                            # Safe to send here (and ONLY here): this is a
                            # `requires_action` idle, i.e. the session is
                            # NOT running. A bare `user.*` event sent into a
                            # RUNNING session returns HTTP 200 and is
                            # silently ignored (measured 2026-08-26) -- never
                            # move this send to a running-session position.
                            await _send_decision_batch(
                                decisions,
                                fresh=fresh,
                                cancel=cancel,
                                io=io,
                                session_id=session_id,
                            )
                            log.info(
                                "turn.tool_confirmation.sent",
                                session_id=session_id,
                                count=len(fresh),
                            )
                            continue
                        if seen_before or pending_confirmation_ids(
                            event.stop_reason, confirmed=accepted_tool_use_ids
                        ):
                            # MA repeats a `requires_action` idle while the
                            # paused batch's other tools run, and pauses once
                            # per queued confirmation. A pause naming a call
                            # with no tool result yet, or an event ID already seen in
                            # stream/replay is a duplicate: keep reading.
                            # If MA never takes it, the stream goes quiet and
                            # the eventless-cycle check ends the turn.
                            log.info(
                                "turn.tool_confirmation.stale_idle",
                                session_id=session_id,
                                event_id=event.id,
                            )
                            continue
                        # MA took our confirmations and asked again for the
                        # same ids -- stop instead of spinning until the
                        # ceiling (T-19-08-C).
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
        if next_task is not None and not next_task.done():
            next_task.cancel()
            with _suppress_task_exc():
                await next_task
        if opened_stream is not None:
            # Unconditional close of any stream this attempt opened, on
            # every exit path -- clean close, read timeout, mid-consume
            # cancel, and (new) a ceiling `wait_for` cancellation landing
            # here all leave an open SSE response otherwise.
            # `httpx.Response.aclose()` is idempotent, so this is safe even
            # when an explicit path above already closed it. Cleanup
            # boundary (guideline:architecture): a transport that is
            # already broken must not replace the real exception with a
            # close failure.
            with contextlib.suppress(Exception):
                await opened_stream.close()


# --- Finalizers ----------------------------------------------------------


async def _finalize_success_or_error(
    *,
    normalized_reason: TerminationReason | None = None,
    state_cell: list[TurnState],
    lifecycle: TurnLifecycle,
    render_once: RenderOnce,
    session_id: str,
    events_folded: int,
    renders_failed: int,
    tool_confirmation: ToolConfirmation,
) -> TurnState:
    final_state = state_cell[0]
    if (
        final_state.error is None
        and final_state.stop_reason is not None
        and final_state.stop_reason.type == "requires_action"
    ):
        # A held approval, a repeated request, or a missing echo can leave the
        # turn here. The person sees the same actionable message in each case.
        match tool_confirmation:
            case AutoApprove() | PolicyApproval():
                message = "The agent stopped because it couldn't confirm your approval."
            case RequireApproval():
                message = "Approvals aren't available here. Ask in Discord, Slack or Teams."
        err = TurnError(kind="requires_action", message=message)
        final_state = dataclasses.replace(
            final_state, error=err, termination=TerminationReason.REQUIRES_ACTION
        )
        state_cell[0] = final_state
    if final_state.error is None:
        # #79: an MCP failure the reducer kept out of `error` degrades a turn
        # that still answered. A turn that answered with nothing is dead
        # (MA's `exhausted` means exactly that), so name the server here
        # instead of surfacing a blank success. A `retrying` error that was
        # never settled is surfaced when nothing was produced, or when MA's
        # own stop reason says the retries ran out behind a partial answer.
        retries_exhausted = (
            final_state.stop_reason is not None
            and final_state.stop_reason.type == "retries_exhausted"
        )
        if not final_state.content and final_state.mcp_failures:
            err = TurnError(
                kind="upstream",
                message=degraded_failure_message(final_state.mcp_failures),
                cause=final_state.mcp_failures[-1],
            )
            final_state = dataclasses.replace(
                final_state, error=err, termination=TerminationReason.MCP_DEGRADED_EMPTY
            )
            state_cell[0] = final_state
        elif final_state.retrying_error is not None and (
            not final_state.content or retries_exhausted
        ):
            final_state = dataclasses.replace(
                final_state,
                error=final_state.retrying_error,
                termination=TerminationReason.RETRYING_UNSETTLED,
            )
            state_cell[0] = final_state
    if final_state.termination is None:
        # Legacy treats a retries_exhausted idle with no recorded failure as
        # completed. Preserve that M0 behavior: a normalized failure cannot
        # create a new host error or change its outcome without the lead's
        # explicit intentional-diff decision.
        # The reducer names the ends only it can see (MA terminating the
        # session); everything else follows from the error, or its absence.
        final_state = dataclasses.replace(
            final_state,
            termination=normalized_reason
            if normalized_reason in {TerminationReason.COMPLETED, TerminationReason.INTERRUPTED}
            and final_state.error is None
            else termination_reason(final_state.error),
        )
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
        if (
            final_state.content
            and final_state.stop_reason is not None
            and final_state.stop_reason.type == "end_turn"
        ):
            await acknowledge(lifecycle, "done")
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
    state_cell[0] = dataclasses.replace(
        state_cell[0],
        error=turn_err,
        stop_reason=None,
        termination=TerminationReason.CONNECTION_LOST,
    )
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
        termination=(
            TerminationReason.RATE_LIMITED
            if isinstance(err, _anthropic.RateLimitError)
            or (isinstance(err, ProviderError) and err.category == "rate_limited")
            else TerminationReason.UPSTREAM
        ),
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
    state_cell[0] = dataclasses.replace(
        state_cell[0],
        error=turn_err,
        stop_reason=None,
        termination=TerminationReason.INTERRUPTED,
    )
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
    io: TurnIO,
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
        stop = await io.interrupt(timeout_s=interrupt_timeout_s)
    except TurnError as err:
        # send_interrupt_and_wait raises TurnError(kind="interrupt_timeout")
        # on its timeout; propagate through the on_terminal_failure path.
        log.warning(
            "turn.interrupt.timeout",
            session_id=session_id,
            timeout_s=interrupt_timeout_s,
        )
        state_cell[0] = dataclasses.replace(
            state_cell[0], error=err, termination=termination_reason(err)
        )
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

    reason = TerminationReason.INTERRUPTED if stop is None else stop_termination_reason(stop)
    if reason.is_failure:
        error = TurnError(
            kind="upstream",
            message=(
                "session terminated by MA"
                if reason == TerminationReason.SESSION_TERMINATED
                else "MA ended the turn without confirming the interruption"
            ),
        )
        state_cell[0] = dataclasses.replace(state_cell[0], error=error, termination=reason)
        await render_once(state_cell[0])
        await lifecycle.on_terminal_failure(state_cell[0], error)
        return state_cell[0]

    log.info("turn.interrupt.sent", session_id=session_id)
    await lifecycle.on_interrupt_sent("cancel_event")
    log.info("turn.interrupt.acked", session_id=session_id)
    # Ack arrived -- partial state is "clean" (refinements §5).
    state_cell[0] = dataclasses.replace(state_cell[0], termination=reason)
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
