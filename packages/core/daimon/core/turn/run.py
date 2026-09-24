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
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import anthropic as _anthropic
import structlog
from anthropic.types import RawMessageStreamEvent
from anthropic.types.beta.beta_managed_agents_system_content_block_param import (
    BetaManagedAgentsSystemContentBlockParam,
)
from anthropic.types.beta.sessions import BetaManagedAgentsImageBlockParam
from daimon.core.errors import TurnError
from daimon.core.handoff_context import (
    render_lost_workspace_framing,
    render_previous_session,
    select_recent_turns,
)
from daimon.core.ma import replay_events
from daimon.core.session_preparation_stages import lock_preparation
from daimon.core.stores.domain import TransferKind
from daimon.core.stores.thread_session_lineage import link_replacement
from daimon.core.stores.thread_sessions import (
    get_live_thread_session,
    get_thread_session_by_id,
    mark_dead,
)
from daimon.core.turn.ceiling import ceiling_error, remaining_s, turn_deadline
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.driver import run_turn
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason, TurnLifecycle
from daimon.core.turn.posture import Billed
from daimon.core.turn.prepare import (
    ContinuityOutcome,
    CreatedSession,
    PreparedTurn,
    bind_recorder,
    create_ma_session,
    insert_mapping,
)
from daimon.core.turn.state import TurnState

log = structlog.get_logger(__name__)

__all__ = ["RunOutcome", "run_prepared_turn"]


@dataclass(frozen=True)
class RunOutcome:
    """The final `TurnState` plus the session/mapping ids the FINAL attempt
    ran against -- needed because the adapter still owns the watermark
    write, which must target the post-recovery mapping_id.

    `continuity` is what the adapter should say happened to the session. It is
    the bound `PreparedTurn`'s outcome unless recovery recreated the session
    mid-call, in which case it is that outcome restated as
    `replaced_after_loss`: the workspace was lost rather than deliberately
    replaced, and the copy must not claim the work came across. Its
    `transfer_kind` then says how much of the old session could be rescued --
    `transcript` when its event log was still readable, `history` when it was
    not -- and `user_prefix` / `system_blocks` are what the replacement was
    actually told, not what the overtaken bind had decided.
    """

    state: TurnState
    ma_session_id: str
    mapping_id: uuid.UUID | None
    recovered: bool
    continuity: ContinuityOutcome = ContinuityOutcome()


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
    `fresh_cancel`: Discord's new `CancelView` lands with the recovery
    lifecycle's next flush, while the Slack adapter re-registers the adopted
    card's entry eagerly, at construction -- a click on the ORIGINAL
    affordance in the window between `fresh_cancel` being created and
    whichever rebind lands would otherwise set an event nothing is watching.
    This task closes that window (and also covers any future adapter that
    forgets to rebind).
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


async def _replay_previous_session(
    anthropic: _anthropic.AsyncAnthropic,
    *,
    session_id: str,
    from_agent_name: str,
) -> str | None:
    """The lost session's conversation as a quoted block, or None.

    Reading it is the difference between a successor that knows what the task
    was and one that only sees whatever the platform thread happens to show.
    Which of the two a given loss gets is decided by MA: an ARCHIVED session's
    event log stays fully listable (capability matrix P9.d), a DELETED one's
    is gone with it (P9.e), and both reach this function as the same dead
    signature.

    A failure here is not a failed turn: this whole path exists to heal a
    thread that is already broken, so an unreadable log degrades to the
    history rung rather than surfacing.
    """
    try:
        events = await replay_events(anthropic, session_id=session_id)
    except (_anthropic.APIError, TurnError) as err:
        log.info(
            "turn.recovery_transcript_unavailable",
            session_id=session_id,
            error=str(err)[:200],
        )
        return None
    turns = select_recent_turns(events)
    if not turns:
        return None
    return render_previous_session(turns, from_agent_name=from_agent_name)


def _with_prefix(prefix: str, message: str) -> str:
    """The user message actually sent, with the successor's framing in front.

    One newline, and only when there is a prefix: an ordinary turn must send
    exactly the bytes it always did.
    """
    return f"{prefix}\n{message}" if prefix else message


@dataclass(frozen=True, slots=True)
class _Replacement:
    """The session a recovering turn moves onto after its own died."""

    ma_session_id: str
    mapping_id: uuid.UUID
    model_id: str
    transfer_kind: TransferKind
    previous_session: str | None
    adopted: bool
    """True when another turn had already replaced the dead session."""


_ORPHAN_ARCHIVE_TIMEOUT_S = 5.0
"""How long a rolled-back recovery waits to archive the session it created.
Short: the caller is usually unwinding a ceiling or a cancel."""


async def _archive_orphaned_session(
    anthropic: _anthropic.AsyncAnthropic, *, session_id: str
) -> None:
    """Best-effort archive of an upstream session no mapping row names.

    Shielded from caller cancellation so it can wait for the request up to a
    fixed deadline. If that deadline expires, the request continues in the
    background and its result is logged when it finishes. A failure is
    swallowed: the error being unwound is the one the caller must see.
    """
    archive_task = asyncio.create_task(
        anthropic.beta.sessions.archive(session_id), name="turn.orphan_session_archive"
    )
    deadline = asyncio.get_running_loop().time() + _ORPHAN_ARCHIVE_TIMEOUT_S

    def log_late_result(task: asyncio.Task[object]) -> None:
        if task.cancelled():
            return
        err = task.exception()
        if err is not None:
            log.warning(
                "turn.recovery_orphan_archive_failed",
                session_id=session_id,
                error=str(err)[:200],
            )
        else:
            log.info("turn.recovery_orphan_archived", session_id=session_id)

    try:
        while True:
            remaining_s = deadline - asyncio.get_running_loop().time()
            try:
                done, _ = await asyncio.wait((archive_task,), timeout=max(remaining_s, 0))
            except asyncio.CancelledError:
                # Keep the original unwind error authoritative, and keep
                # waiting for the archive until the fixed deadline.
                continue
            if not done:
                archive_task.add_done_callback(log_late_result)
                log.warning(
                    "turn.recovery_orphan_archive_failed",
                    session_id=session_id,
                    error="archive wait timed out",
                )
                return
            if archive_task.cancelled():
                log.warning(
                    "turn.recovery_orphan_archive_failed",
                    session_id=session_id,
                    error="archive request cancelled",
                )
                return
            archive_task.result()
    except Exception as err:
        log.warning(
            "turn.recovery_orphan_archive_failed",
            session_id=session_id,
            error=str(err)[:200],
        )
    else:
        log.info("turn.recovery_orphan_archived", session_id=session_id)


async def _replace_dead_session(
    deps: TurnDeps,
    prepared: PreparedTurn,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    dead_session_id: str,
    dead_mapping_id: uuid.UUID,
) -> _Replacement:
    """Mark the dead mapping dead and move onto one live replacement.

    Runs under the same per-(tenant, platform, thread, account) advisory lock
    as `prepare_session_for_turn`. Two turns can be running on one mapping
    (a Discord wizard submit does not queue behind a mention), and both see
    the session die. Without the lock each marked the row dead and created
    its own replacement, leaving two live rows for one thread; a bind landing
    between the mark and the create did the same. Under the lock the second
    recovery finds the first one's replacement live and adopts it, and a bind
    sees either the old row or the replacement, never neither.
    """
    admission = prepared.admission
    session_account_id = prepared.session_account_id
    created: CreatedSession | None = None
    try:
        async with deps.sessionmaker() as db, db.begin():
            await lock_preparation(
                db,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                account_id=session_account_id,
            )
            dead_row = await get_thread_session_by_id(db, id=dead_mapping_id)
            live = await get_live_thread_session(
                db,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                account_id=session_account_id,
            )
            await mark_dead(db, id=dead_mapping_id)

            if live is not None and live.id != dead_mapping_id:
                snapshot = live.effective_config
                return _Replacement(
                    ma_session_id=live.ma_session_id,
                    mapping_id=live.id,
                    model_id=(
                        snapshot.model_id if snapshot is not None else admission.agent.model.id
                    ),
                    transfer_kind=live.transfer_kind or "history",
                    previous_session=None,
                    adopted=True,
                )

            # Whose conversation it was, for the quoted block's `from` attribute:
            # the snapshot the dead session actually froze, or the responder
            # resolved for this turn on a row written before snapshots existed.
            dead_snapshot = dead_row.effective_config if dead_row is not None else None
            from_agent_name = (
                dead_snapshot.agent_name if dead_snapshot is not None else admission.agent.name
            )
            previous_session = await _replay_previous_session(
                deps.anthropic,
                session_id=dead_session_id,
                from_agent_name=from_agent_name,
            )

            # What the successor actually inherited, recorded on its row as the
            # rung it came in on: `transcript` when the archived log read,
            # `history` when nothing of the old session was left to read.
            loss_transfer_kind: TransferKind = (
                "transcript" if previous_session is not None else "history"
            )
            # The upstream session first, then its row IN this locked transaction:
            # a cancel or ceiling landing anywhere up to the commit rolls back the
            # dead-mark, the replacement row and the link together, instead of
            # leaving a live, unlinked replacement the next mention would continue
            # on without the lost-workspace framing.
            created = await create_ma_session(deps, admission, tenant_id=tenant_id)
            fresh = await insert_mapping(
                db,
                created,
                admission,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                session_account_id=session_account_id,
                predecessor_id=dead_mapping_id,
                transfer_kind=loss_transfer_kind,
            )

            # Close the chain from the other end. The dead row keeps
            # `status="dead"` -- it says how this session ended, which a supersede
            # would overwrite -- and gains only the pointer forward.
            await link_replacement(db, id=dead_mapping_id, replaced_by_id=fresh.mapping_id)
    except BaseException:
        # The locked transaction rolled back after the upstream session was
        # created (a ceiling, a cancel, a failed insert or commit). No row
        # names that session any more, so nothing else would ever archive it.
        if created is not None:
            await _archive_orphaned_session(deps.anthropic, session_id=created.ma_session_id)
        raise

    return _Replacement(
        ma_session_id=fresh.ma_session_id,
        mapping_id=fresh.mapping_id,
        model_id=fresh.snapshot.model_id,
        transfer_kind=loss_transfer_kind,
        previous_session=previous_session,
        adopted=False,
    )


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

    When the bind produced a replacement session, `prepared.continuity`
    carries what that session has to be told before it can answer: the framing
    prefix goes in front of the user message, and the daimon-authored system
    blocks ride the same first send. Adapters pass nothing for this -- they
    already hand over the `PreparedTurn` that carries it.

    The recovery cycle does the same job for the session it creates after a
    loss, from scratch: the dead session's event log is read back first (still
    listable while MA has only archived it), quoted into the reseeded user
    message, and described by daimon's own framing on the `system.message`
    channel where the replacement's model takes one. The bind's framing is
    dropped rather than forwarded -- it describes the workspace that just died.

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
    continuity_cell: list[ContinuityOutcome] = [prepared.continuity]

    # A replacement session's first user message opens with daimon's framing
    # for the work it inherited (`daimon.core.handoff_context`). Empty for
    # every ordinary turn, which then sends byte-identical bytes to before.
    prefix = prepared.continuity.user_prefix

    async def _run() -> RunOutcome:
        ma_session_id = prepared.ma_session_id
        mapping_id = prepared.mapping_id

        first_attempt = _DeferredFailureLifecycle(inner=lifecycle)
        state = await run_turn(
            anthropic=deps.anthropic,
            session_id=ma_session_id,
            user_message=_with_prefix(prefix, user_message),
            lifecycle=first_attempt,
            cancel=cancel,
            render_interval_s=render_interval_s,
            billing=Billed(record=prepared._record),  # pyright: ignore[reportPrivateUsage]
            image_blocks=image_blocks,
            system_blocks=prepared.continuity.system_blocks,
        )

        if not (_is_dead_session(state) and mapping_id is not None):
            await first_attempt.flush_held_failure()
            return RunOutcome(
                state=state,
                ma_session_id=ma_session_id,
                mapping_id=mapping_id,
                recovered=False,
                continuity=prepared.continuity,
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
                continuity=prepared.continuity,
            )

        # If recovery itself blows up, the withheld failure is the only thing
        # the user would ever see -- without this the embed sits on "thinking"
        # forever, which is the exact failure mode this module exists to
        # prevent. Re-raise: the caller still needs to know recovery broke.
        try:
            recovery = await _replace_dead_session(
                deps,
                prepared,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                dead_session_id=ma_session_id,
                dead_mapping_id=mapping_id,
            )
            new_session_id = recovery.ma_session_id
            new_mapping_id = recovery.mapping_id
            loss_transfer_kind = recovery.transfer_kind
            previous_session = recovery.previous_session

            active_session_id_cell[0] = new_session_id
            active_mapping_id_cell[0] = new_mapping_id
            recovered_cell[0] = True

            # The session this turn was bound to is gone, so whatever the bind
            # decided has been overtaken: this is a replacement after a loss.
            # No files cross -- a bundle the bind mounted was mounted on the
            # session that just died -- but the conversation still can, quoted,
            # when MA only archived the old session rather than deleting it.
            # `transcript` when that read worked, `history` when it did not --
            # the same two rungs `workspace_transfer` walks, so the copy means
            # the same thing on both paths.
            #
            # An adopted replacement was framed by the turn that created it;
            # telling it about the same loss a second time would only repeat
            # the quoted conversation, so this turn sends its message bare.
            if recovery.adopted:
                loss_user_prefix = ""
                loss_system_blocks: tuple[BetaManagedAgentsSystemContentBlockParam, ...] = ()
            else:
                loss_framing = render_lost_workspace_framing(
                    model_id=recovery.model_id,
                    previous_session=previous_session,
                )
                loss_user_prefix = loss_framing.user_prefix
                loss_system_blocks = (
                    loss_framing.system.blocks if loss_framing.system is not None else ()
                )
            continuity_cell[0] = replace(
                prepared.continuity,
                state="replaced_after_loss",
                transfer_kind=loss_transfer_kind,
                user_prefix=loss_user_prefix,
                system_blocks=loss_system_blocks,
            )

            # Bill the REPLACEMENT's own model: it froze the responder agent as
            # it stands now, which need not be what the dead session ran.
            new_record = bind_recorder(
                deps,
                tenant_id=tenant_id,
                external_user_id=external_user_id,
                ma_session_id=new_session_id,
                model_id=recovery.model_id,
            )

            log.info(
                "turn.session_recovered",
                old_session_id=ma_session_id,
                new_session_id=new_session_id,
                old_mapping_id=str(mapping_id),
                new_mapping_id=str(new_mapping_id),
                adopted=recovery.adopted,
                thread_id=thread_id,
            )

            # The bind's own framing is deliberately dropped here: it
            # describes a workspace that no longer exists, and on the full
            # rung it points at a bundle mounted on the session that just
            # died. What goes instead is this loss's own framing -- daimon's
            # words on the privileged channel where the replacement's model
            # takes one, the quoted conversation always in the user message.
            reseeded_message = _with_prefix(loss_user_prefix, await reseed_user_message())
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
                    system_blocks=loss_system_blocks,
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
            continuity=continuity_cell[0],
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
            continuity=continuity_cell[0],
        )
