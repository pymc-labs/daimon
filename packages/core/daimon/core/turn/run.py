"""D-08/D-09/D-10: `run_prepared_turn` owns the driver call and the one-shot
dead-session recovery cycle.

`_is_dead_session` is ported verbatim from
`daimon.adapters.discord.bot._is_dead_session` (D-10) -- applies uniformly to
fresh and reused sessions; a fresh-session 404 costs one harmless retry
rather than adding a reused-only guard (an unnamed behaviour change that was
considered and rejected).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import anthropic as _anthropic
import structlog
from anthropic.types import RawMessageStreamEvent
from anthropic.types.beta.sessions import BetaManagedAgentsImageBlockParam
from daimon.core.stores.thread_sessions import mark_dead
from daimon.core.turn.ceiling import ceiling_error, remaining_s, turn_deadline
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.driver import run_turn
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason, TurnLifecycle
from daimon.core.turn.posture import Billed
from daimon.core.turn.prepare import PreparedTurn, bind_recorder, create_fresh_session
from daimon.core.turn.state import TurnState

log = structlog.get_logger(__name__)

__all__ = ["RunOutcome", "run_prepared_turn"]


@dataclass(frozen=True)
class RunOutcome:
    """The final `TurnState` plus the session/mapping ids the FINAL attempt
    ran against -- needed because the adapter still owns the watermark
    write, which must target the post-recovery mapping_id.
    """

    state: TurnState
    ma_session_id: str
    mapping_id: uuid.UUID | None
    recovered: bool


# MA's rejection when events.send targets a session it has terminated. The id
# is well-formed and the session exists — it is simply closed to new events, so
# this is a 400 rather than the 404 a deleted session gives.
_ARCHIVED_SESSION_MARKER = "cannot send events to archived session"


@dataclass
class _DeferredFailureLifecycle:
    """First-attempt wrapper that holds the terminal-failure hook until we know
    whether recovery will run.

    A dead-session 400 is recoverable and heals in about three seconds, but the
    Discord adapter renders its red error embed from ``on_terminal_failure``
    (despite the protocol calling that hook bookkeeping-only), so the user sees
    a scary ``upstream: Error code: 400`` that is retracted a moment later. The
    adopted message ref means the error does not *persist*; holding the call is
    what stops it being *shown*. Replayed verbatim when we do not recover, so a
    genuinely failed turn is unaffected.
    """

    inner: TurnLifecycle
    _held: tuple[TurnState, Exception] | None = None

    async def on_render(self, state: TurnState) -> None:
        await self.inner.on_render(state)

    async def on_terminal_success(self, state: TurnState) -> None:
        await self.inner.on_terminal_success(state)

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        self._held = (state, err)

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        await self.inner.on_sse_event(event)

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        await self.inner.on_reconnect(reason)

    async def on_rate_limited(self, until: datetime | None) -> None:
        await self.inner.on_rate_limited(until)

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        await self.inner.on_interrupt_sent(source)

    async def flush_held_failure(self) -> None:
        """Replay the withheld failure. Call on every path that does not recover."""
        if self._held is None:
            return
        state, err = self._held
        self._held = None
        await self.inner.on_terminal_failure(state, err)


async def _mirror_cancel(cancel: asyncio.Event, fresh_cancel: asyncio.Event) -> None:
    """Forward a LATE cancel on the ORIGINAL event into `fresh_cancel` for the
    duration of the recovery turn (D-07(b)).

    The adapters' recovery lifecycles rebind their cancel affordance to
    `fresh_cancel` (Discord's new `CancelView`, Slack's re-registration), but
    only once their next flush lands -- a click on the ORIGINAL affordance in
    the window between `fresh_cancel` being created and that rebind landing
    would otherwise set an event nothing is watching. This task closes that
    window (and also covers any future adapter that forgets to rebind).
    """
    await cancel.wait()
    fresh_cancel.set()


def _is_dead_session(state: TurnState) -> bool:
    """Return True if state.error signals a gone or closed MA session.

    Two distinct signatures, both meaning "this session can never accept
    another event, so recreate rather than surfacing a dead end":

    - **404** from events.send: the session existed but is gone (deleted /
      expired / GC'd).
    - **400 whose message is `Cannot send events to archived session`**: MA
      terminated the session (e.g. a turn hit a terminal model error) and
      closed it to further events.

    Every OTHER 400 still surfaces as a normal turn error. That distinction is
    the point: a bare 400 means a malformed session id, which is not reachable
    with well-formed stored ids and must not trigger a recreate. Matching on
    the message rather than the status alone keeps that case excluded.

    The 400 limb is why this function exists in its current shape. Without it a
    single terminal error bricked the thread PERMANENTLY: MA terminated the
    session, the mapping row still pointed at it, and every later message in
    that thread 400'd here forever with zero tokens billed and no path back.
    Observed on staging thread 1535185295245582356 / session
    sesn_01TBcsjhyD4KMEc6wasC3vyg (2026-08-07), where an oversized image ended
    the session and the next "hello" — and every message after it — failed.

    Note this deliberately does NOT fire on the terminating turn itself (whose
    error is `session terminated by MA`, carrying no APIStatusError cause).
    That turn really did fail, and re-running it against a fresh session would
    just replay whatever killed it. Recovery instead happens on the NEXT
    message, which is the first to see the archived-session 400 — so the thread
    heals on its own without retrying poison.

    D-10: a `kind == "ceiling"` error must NEVER recover here either -- it is
    excluded by construction (this function only ever returns True for
    `kind == "upstream"`), and that exclusion is what stops a 45-minute
    wall-clock timeout from being re-run as a second 45-minute turn.
    """
    err = state.error
    if err is None or err.kind != "upstream":
        return False
    cause = err.cause
    if not isinstance(cause, _anthropic.APIStatusError):
        return False
    if cause.status_code == 404:
        return True
    return cause.status_code == 400 and _ARCHIVED_SESSION_MARKER in str(cause).lower()


async def run_prepared_turn(
    deps: TurnDeps,
    prepared: PreparedTurn,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    external_user_id: str,
    user_message: str,
    lifecycle: TurnLifecycle,
    cancel: asyncio.Event,
    reseed_user_message: Callable[[], Awaitable[str]],
    recovery_lifecycle: Callable[[asyncio.Event], TurnLifecycle],
    image_blocks: Sequence[BetaManagedAgentsImageBlockParam] | None = None,
    render_interval_s: float = 2.0,
    deadline: datetime | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RunOutcome:
    """Run one turn against `prepared`'s session; on a dead-session (404)
    signature, recover exactly once: mark the stale mapping dead, create a
    fresh session + mapping row, rebind the recorder to the NEW session id,
    reseed the user message, and re-run once. A second consecutive dead
    signature is returned as-is -- no further retry.

    `external_user_id` is required here (not carried on `PreparedTurn`)
    because recovery must rebuild the usage recorder from scratch against
    the new session id via `prepare.py`'s binding helper, which needs the
    platform user id explicitly, exactly as `bind_session` did to build the
    original recorder.

    `deadline`/`now` bound the WHOLE body -- first attempt, recovery setup,
    and the recovery re-run -- against the per-turn ceiling (D-08/D-09).
    `deadline=None` is fail-safe, not off: it computes
    `turn_deadline(now=now())` so a caller that never passes a deadline is
    still ceiling-covered. On breach: the mapping id the FINAL attempt was
    running against (tracked as it moves through recovery) is marked dead
    (D-09, relocated from Discord's `_retire_deadlocked_turn`) so the next
    mention does not bind the same wedged session, the caller's own
    `lifecycle.on_terminal_failure` is invoked directly (the driver's
    finalizers never ran, so nothing else would render this), and a
    `RunOutcome` carrying `state.error.kind == "ceiling"` is returned rather
    than raised -- adapters take their existing `state.error is not None`
    branch with no new code. D-10: this can never be misread as a dead-session
    signal (`_is_dead_session` gates on `kind == "upstream"`), so a ceiling
    breach can never loop into a second wall-clock-priced attempt.
    """
    effective_deadline = deadline if deadline is not None else turn_deadline(now=now())

    # Tracks the session/mapping id (and whether recovery has taken over) the
    # FINAL attempt is running against, updated by `_run` the instant recovery
    # recreates -- so a ceiling breach mid-recovery marks the NEW mapping dead,
    # not the stale one already marked dead by the ordinary recovery cycle.
    active_session_id_cell: list[str] = [prepared.ma_session_id]
    active_mapping_id_cell: list[uuid.UUID | None] = [prepared.mapping_id]
    recovered_cell: list[bool] = [False]

    async def _run() -> RunOutcome:
        ma_session_id = prepared.ma_session_id
        mapping_id = prepared.mapping_id

        first_attempt = _DeferredFailureLifecycle(inner=lifecycle)
        state = await run_turn(
            anthropic=deps.anthropic,
            session_id=ma_session_id,
            user_message=user_message,
            lifecycle=first_attempt,
            cancel=cancel,
            render_interval_s=render_interval_s,
            billing=Billed(record=prepared._record),  # pyright: ignore[reportPrivateUsage]
            image_blocks=image_blocks,
        )

        if not (_is_dead_session(state) and mapping_id is not None):
            await first_attempt.flush_held_failure()
            return RunOutcome(
                state=state,
                ma_session_id=ma_session_id,
                mapping_id=mapping_id,
                recovered=False,
            )

        # D-07(a): a cancel already signalled by the time we'd start recovery
        # means the user asked to stop before we ever attempted a second full
        # agentic turn -- running one anyway would bill work nobody wanted.
        # Abort recovery and surface the withheld first-attempt failure as
        # the final outcome, same shape as the non-recoverable branch above.
        if cancel.is_set():
            await first_attempt.flush_held_failure()
            return RunOutcome(
                state=state,
                ma_session_id=ma_session_id,
                mapping_id=mapping_id,
                recovered=False,
            )

        # If recovery itself blows up, the withheld failure is the only thing
        # the user would ever see -- without this the embed sits on "thinking"
        # forever, which is the exact failure mode this module exists to
        # prevent. Re-raise: the caller still needs to know recovery broke.
        try:
            async with deps.sessionmaker() as session:
                await mark_dead(session, id=mapping_id)
                await session.commit()

            new_session_id, new_mapping_id = await create_fresh_session(
                deps,
                prepared.admission,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                session_account_id=prepared.session_account_id,
            )
            active_session_id_cell[0] = new_session_id
            active_mapping_id_cell[0] = new_mapping_id
            recovered_cell[0] = True

            new_record = bind_recorder(
                deps,
                prepared.admission,
                tenant_id=tenant_id,
                external_user_id=external_user_id,
                ma_session_id=new_session_id,
            )

            log.info(
                "turn.session_recovered",
                old_session_id=ma_session_id,
                new_session_id=new_session_id,
                old_mapping_id=str(mapping_id),
                new_mapping_id=str(new_mapping_id),
                thread_id=thread_id,
            )

            reseeded_message = await reseed_user_message()
            fresh_cancel = asyncio.Event()
            new_lifecycle = recovery_lifecycle(fresh_cancel)

            # D-07(b): mirror a LATE cancel on the ORIGINAL event into
            # `fresh_cancel` for the duration of the recovery turn -- see
            # `_mirror_cancel`'s docstring for the window this closes.
            mirror_task = asyncio.create_task(
                _mirror_cancel(cancel, fresh_cancel), name="turn.cancel_mirror"
            )
            try:
                recovered_state = await run_turn(
                    anthropic=deps.anthropic,
                    session_id=new_session_id,
                    user_message=reseeded_message,
                    lifecycle=new_lifecycle,
                    cancel=fresh_cancel,
                    render_interval_s=render_interval_s,
                    billing=Billed(record=new_record),
                    image_blocks=image_blocks,
                )
            finally:
                if not mirror_task.done():
                    mirror_task.cancel()
                    with contextlib.suppress(BaseException):
                        await mirror_task
        except Exception:
            await first_attempt.flush_held_failure()
            raise

        return RunOutcome(
            state=recovered_state,
            ma_session_id=new_session_id,
            mapping_id=new_mapping_id,
            recovered=True,
        )

    try:
        return await asyncio.wait_for(_run(), timeout=remaining_s(effective_deadline, now=now()))
    except TimeoutError:
        active_session_id = active_session_id_cell[0]
        active_mapping_id = active_mapping_id_cell[0]
        recovered = recovered_cell[0]
        err = ceiling_error()

        log.error(
            "turn.ceiling_exceeded",
            phase="run_prepared_turn",
            session_id=active_session_id,
            mapping_id=str(active_mapping_id) if active_mapping_id is not None else None,
            thread_id=thread_id,
            platform=platform,
            deadline=effective_deadline.isoformat(),
        )

        if active_mapping_id is not None:
            async with deps.sessionmaker() as session:
                await mark_dead(session, id=active_mapping_id)
                await session.commit()

        try:
            await lifecycle.on_terminal_failure(TurnState(error=err), err)
        except Exception as render_err:
            # Rendering is delivery, not correctness -- a broken adapter hook
            # must not mask the ceiling error itself.
            log.warning("turn.ceiling_render_failed", error=str(render_err))

        return RunOutcome(
            state=TurnState(error=err),
            ma_session_id=active_session_id,
            mapping_id=active_mapping_id,
            recovered=recovered,
        )
