"""`run_prepared_turn` owns the driver call and the one-shot
dead-session recovery cycle.

`_is_dead_session` is ported verbatim from
`daimon.adapters.discord.bot._is_dead_session` -- applies uniformly to
fresh and reused sessions; a fresh-session 404 costs one harmless retry
rather than adding a reused-only guard (an unnamed behaviour change that was
considered and rejected).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict, cast

import anthropic as _anthropic
import structlog
from anthropic.types import RawMessageStreamEvent
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_system_content_block_param import (
    BetaManagedAgentsSystemContentBlockParam,
)
from anthropic.types.beta.sessions import BetaManagedAgentsImageBlockParam
from daimon.core.config import load_turn_settings
from daimon.core.confirmation import (
    ConfirmationHook,
    ToolRuleCardReference,
    confirmed_tools_for_tenant,
)
from daimon.core.context_prompt import TurnContext, context_prompt
from daimon.core.context_provider import ContextHook, ContextRequest, LoadedProvider
from daimon.core.errors import TurnError
from daimon.core.handoff_context import (
    render_lost_workspace_framing,
    render_previous_session,
    select_recent_turns,
    supports_system_message,
)
from daimon.core.ma import replay_events
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_tool_annotations import agent_mcp_token_for, mcp_annotation_lookup
from daimon.core.model_presets import model_choice_id
from daimon.core.mux_backend import TurnBackendRequest, TurnRuntime
from daimon.core.mux_compat import legacy_call
from daimon.core.session_fence_retry import retry_fences
from daimon.core.session_mutation import SessionRetired, session_mutation_fence
from daimon.core.session_preparation_gate import pool_headroom
from daimon.core.session_preparation_stages import lock_preparation
from daimon.core.session_snapshot import session_mcp_servers
from daimon.core.stores.domain import TaskContinuationRow, TransferKind
from daimon.core.stores.gemini_runtime_state import complete
from daimon.core.stores.thread_session_lineage import link_replacement
from daimon.core.stores.thread_sessions import (
    get_live_thread_session,
    get_thread_session_by_id,
    mark_dead,
    transfer_active_turn,
)
from daimon.core.stores.turn_origins import bind_origin_session
from daimon.core.thread_models import MODEL_FALLBACK_NOTICE
from daimon.core.tool_safety import daimon_endpoint, trusted_servers_for
from daimon.core.turn.admission import (
    Admission,
    decide_before_send,
    reauthorize,
    refresh_approval_gate,
    require_current_spend,
)
from daimon.core.turn.approvals import (
    agent_rule_lookup,
    channel_for_tool_rules,
    chat_tool_confirmation,
    unattended_decider,
)
from daimon.core.turn.binding import prepared_persistence
from daimon.core.turn.ceiling import (
    ceiling_error,
    current_turn_deadline,
    remaining_s,
    turn_deadline,
)
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.driver import run_turn, unattended_sends
from daimon.core.turn.errors import AdmissionDenied, SessionBusyError
from daimon.core.turn.io import LegacyTurnIO, TurnIO, turn_io
from daimon.core.turn.lifecycle import (
    Acknowledgment,
    InterruptSource,
    ReconnectReason,
    TurnLifecycle,
    acknowledge,
)
from daimon.core.turn.outbound import scrubbed_for
from daimon.core.turn.outcomes import TurnObservation, current_outcome
from daimon.core.turn.output import OutputAuthorization, output_authorization
from daimon.core.turn.persistence import BindingSuperseded, LeaseWait, TurnPersistence
from daimon.core.turn.posture import (
    Billed,
    BillingPosture,
    PolicyApproval,
    ToolConfirmation,
    UsageRecorder,
)
from daimon.core.turn.prepare import (
    ContinuityOutcome,
    CreatedSession,
    PreparedTurn,
    admitted_session_scope,
    bind_recorder,
    create_ma_session,
    insert_mapping,
    stamp_session_seal,
)
from daimon.core.turn.progress_plans import progress_plans_prefix
from daimon.core.turn.provider_actions import (
    ProviderActionApproval,
    ProviderActionHook,
    ProviderActionRequester,
)
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason, termination_reason
from daimon.core.turn.unattended import (
    dm_session_authority,
    refuse_ungated_confirmation,
    require_native_unattended_capabilities,
    session_gate,
)
from daimon.core.turn.unattended_capabilities import (
    ServerTool,
    capability_policy,
    capability_posture,
    constrain_capabilities,
    invalidate_cached_tools,
    protected_tools,
    restore_capabilities,
    send_capabilities,
    unattended_entry_point,
)
from daimon.core.turn_origin import current_origin_id, execution_origin
from daimon.core.usage_billing import ObservationBilled, ObservationRecorder
from daimon.core.usage_sweep import SETTLE_BUDGET_S, settle_session
from mux.contracts.events import Event
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.profile import SUPPORTED
from mux.drivers.anthropic import SESSION_READS_NAMESPACE, SessionReads
from mux.errors import ScopeViolation, UnsupportedCapability
from mux.profiles import MANAGED_AGENTS, get_profile
from mux.profiles.selfhosted import LOOP as SELFHOSTED_LOOP
from mux.state.lease import Lease
from sqlalchemy.exc import SQLAlchemyError

_JOB_ALLOWLIST = frozenset({ServerTool("daimon-mcp", "get_long_job")})


class _TurnPortKwargs(TypedDict, total=False):
    path: Literal["legacy", "mux"]
    backend: ManagedAgents
    scope: Scope
    session_ref: ResourceRef
    profile: str
    backend_request: TurnBackendRequest


class _ProviderActionKwargs(TypedDict, total=False):
    provider_actions: ProviderActionApproval


def prepared_billing(prepared: PreparedTurn, *, tenant_id: uuid.UUID) -> BillingPosture:
    """Choose the native recorder only for a proved foreign prepared session."""
    revision = prepared.admission.backend_revision
    profile = revision.profile if revision is not None else "anthropic.managed_agents"
    ref = prepared.session_ref
    if profile == "anthropic.managed_agents":
        if ref is not None and ref.provider != "anthropic":
            raise ScopeViolation(
                prepared.ma_session_id, "recorder differs from the admitted profile"
            )
        return Billed(record=cast(UsageRecorder, prepared._record))  # pyright: ignore[reportPrivateUsage]
    scope = admitted_session_scope(
        prepared.admission, tenant_id=tenant_id, session_id=prepared.ma_session_id
    )
    if ref is None or (
        ref.kind != "session"
        or ref.provider != get_profile(profile).provider
        or ref.id != prepared.ma_session_id
        or ref.tenant_id != scope.tenant_id
        or ref.account_id != scope.account_id
        or revision is None
        or revision.channel.tenant_id != scope.tenant_id
    ):
        raise ScopeViolation(prepared.ma_session_id, "native recorder has no authorized session")
    return ObservationBilled(
        record=cast(ObservationRecorder, prepared._record),  # pyright: ignore[reportPrivateUsage]
        native_footer_cost=prepared.native_footer_cost,
    )


def _turn_port_kwargs(
    deps: TurnDeps,
    admission: Admission,
    session_id: str,
    *,
    tenant_id: uuid.UUID,
    provider_backend: ManagedAgents | None = None,
    provider_session: ResourceRef | None = None,
    provider_runtime: TurnRuntime | None = None,
    provider_scope: Scope | None = None,
) -> _TurnPortKwargs:
    """Derive a real tenant scope from the existing admitted operation."""
    selected = deps.turn_path or load_turn_settings().path
    scope = admitted_session_scope(
        admission, tenant_id=tenant_id, session_id=session_id, prepared=provider_scope
    )
    profile = (
        admission.backend_revision.profile
        if admission.backend_revision is not None
        else "anthropic.managed_agents"
    )
    if selected == "legacy":
        if profile != "anthropic.managed_agents":
            # Forward the explicit selection so the driver refuses native
            # legacy I/O rather than silently routing it to Anthropic.
            return {"path": "legacy", "scope": scope, "profile": profile}
        return (
            {"path": "legacy", "scope": scope}
            if load_turn_settings().path != "legacy"
            else {"scope": scope}
        )
    result: _TurnPortKwargs = {"path": "mux", "scope": scope}
    if profile != "anthropic.managed_agents":
        result["profile"] = profile
    if provider_backend is not None:
        result["backend"] = provider_backend
    elif deps.backend is not None:
        if deps.backend_session_ref is None:
            raise ScopeViolation(session_id, "an injected backend requires a session ref resolver")
        result["backend"] = deps.backend
        result["session_ref"] = deps.backend_session_ref(session_id, scope)
    if provider_session is not None:
        result["session_ref"] = provider_session
    injected = result.get("backend")
    if injected is not None and injected.capabilities().profile_id != profile:
        raise ScopeViolation(session_id, "injected backend differs from the admitted profile")
    if profile != "anthropic.managed_agents":
        native = result.get("session_ref")
        if native is None:
            raise ScopeViolation(session_id, "provider turn has no authorized native binding")
        result["backend_request"] = TurnBackendRequest(
            profile=profile,
            client=deps.anthropic,
            scope=scope,
            session_id=session_id,
            deps=deps,
            config=admission.backend_revision,
            session=native,
            runtime=provider_runtime or deps.turn_runtimes.get(profile),
        )
    return result


log = structlog.get_logger(__name__)
#: How long a turn waits for its end-of-turn finalizer (credential revocation)
#: before returning; a slower finalizer keeps running in the background.
_FINALIZE_TIMEOUT_S = 15.0

#: Finalizers still running past their wait: strong references so they are
#: never garbage collected mid-revocation; each removes itself when done.
_PENDING_FINALIZERS: set[asyncio.Future[None]] = set()


def _finalizer_done(task: asyncio.Future[None]) -> None:
    _PENDING_FINALIZERS.discard(task)
    if task.cancelled():
        log.warning("turn.finalize_cancelled")
        return
    error = task.exception()
    if error is not None:
        log.warning("turn.finalize_failed", error=type(error).__name__)


async def _settle(deps: TurnDeps, *, session_id: str, scope: Scope, record: UsageRecorder) -> None:
    """Debit model calls on `session_id` that no live stream metered.

    Runs once the turn is over -- after its answer or failure was rendered --
    so it never delays the user's message or spends the turn's ceiling.
    `settle_session` is bounded by its own page count and budget.

    Named boundary: a failed settle must not fail the user's turn. Recording
    is idempotent, so the next settle on the session retries whatever this one
    could not write.
    """
    try:
        await settle_session(
            deps.anthropic,
            deps.sessionmaker,
            session_id,
            scope=scope,
            record=record,
            deadline=time.monotonic() + SETTLE_BUDGET_S,
        )
    except _anthropic.NotFoundError:
        # A deleted session: its events can no longer be listed.
        log.info("billing.settle_skipped", managed_session_id=session_id)
    except (_anthropic.APIError, SQLAlchemyError):
        log.exception("billing.settle_failed", managed_session_id=session_id)


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

    @property
    def termination(self) -> TerminationReason:
        """How the final attempt ended; the driver sets it on every exit."""
        return self.state.termination or termination_reason(self.state.error)


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
    authorize_final: Callable[[], Awaitable[None]] | None = None

    async def on_acknowledgment(self, phase: Acknowledgment) -> None:
        await acknowledge(self.inner, phase)

    async def on_render(self, state: TurnState) -> None:
        if self.authorize_final is not None:
            # Watches keep their initial card until the final post. Streaming
            # renders would either poll Discord on every tick or deliver stale
            # authorization after access was revoked.
            return
        await self.inner.on_render(state)

    async def _may_finish(self) -> bool:
        if self.authorize_final is None:
            return True
        for delay in (1, 2, None):
            try:
                await self.authorize_final()
                return True
            except Exception as err:
                if not isinstance(err, AdmissionDenied) and delay is not None:
                    await asyncio.sleep(delay)
                    continue
                await self._output_refused(err)
                break
        return False

    async def _output_refused(self, err: Exception) -> None:
        log.warning("turn.final_output_refused", error_type=type(err).__name__)
        cleanup = getattr(self.inner, "on_output_refused", None)
        if cleanup is not None:
            await cast(Callable[[], Awaitable[None]], cleanup)()

    async def on_terminal_success(self, state: TurnState) -> None:
        if await self._may_finish():
            try:
                await self.inner.on_terminal_success(state)
            except Exception as err:
                if self.authorize_final is None or isinstance(err, AdmissionDenied):
                    raise
                await self._output_refused(err)

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        self._held = (state, err)

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        await self.inner.on_sse_event(event)

    async def on_provider_event(self, event: Event) -> None:
        hook = getattr(self.inner, "on_provider_event", None)
        if hook is not None:
            await cast(Callable[[Event], Awaitable[None]], hook)(event)

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        await self.inner.on_reconnect(reason)

    async def on_rate_limited(self, until: datetime | None) -> None:
        await self.inner.on_rate_limited(until)

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        await self.inner.on_interrupt_sent(source)

    async def flush_held_failure(self, termination: TerminationReason | None = None) -> None:
        """Replay the withheld failure. Call on every path that does not recover.

        `termination` restates how the turn ended when the caller learned more
        than the attempt did (the user stopped it before recovery ran).
        """
        if self._held is None:
            return
        state, err = self._held
        self._held = None
        if termination is not None:
            state = replace(state, termination=termination)
        if await self._may_finish():
            try:
                await self.inner.on_terminal_failure(state, err)
            except Exception as delivery_error:
                if self.authorize_final is None or isinstance(delivery_error, AdmissionDenied):
                    raise
                await self._output_refused(delivery_error)


async def _mirror_cancel(cancel: asyncio.Event, fresh_cancel: asyncio.Event) -> None:
    """Forward a LATE cancel on the ORIGINAL event into `fresh_cancel` for the
    duration of the recovery turn.

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

    A `kind == "ceiling"` error must NEVER recover here either -- it is
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
    io: TurnIO | None = None,
    scope: Scope | None = None,
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
        events = (
            await io.replay()
            if io is not None
            else await replay_events(anthropic, session_id=session_id, scope=scope)
        )
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
    admission: Admission | None = None
    """The admission as decided again for the replacement (`reauthorize`)."""


_ORPHAN_ARCHIVE_TIMEOUT_S = 5.0
"""How long a rolled-back recovery waits to archive the session it created.
Short: the caller is usually unwinding a ceiling or a cancel."""


async def _archive_orphaned_session(
    anthropic: _anthropic.AsyncAnthropic,
    *,
    session_id: str,
    io: TurnIO | None = None,
    scope: Scope | None = None,
    archive: Callable[[], Coroutine[object, object, object]] | None = None,
) -> None:
    """Best-effort archive of an upstream session no mapping row names.

    Shielded from caller cancellation so it can wait for the request up to a
    fixed deadline. If that deadline expires, the request continues in the
    background and its result is logged when it finishes. A failure is
    swallowed: the error being unwound is the one the caller must see. A
    profile with no native archive (Gemini inline reuse) has nothing to clean
    up, so its refusal is a skip, never a failure.
    """
    archive_task = asyncio.create_task(
        archive()
        if archive is not None
        else (io or LegacyTurnIO(anthropic, session_id, scope=scope)).archive(),
        name="turn.orphan_session_archive",
    )
    deadline = asyncio.get_running_loop().time() + _ORPHAN_ARCHIVE_TIMEOUT_S

    def log_late_result(task: asyncio.Task[object]) -> None:
        if task.cancelled():
            return
        err = task.exception()
        if _no_archive(err):
            log.info("turn.recovery_orphan_archive_skipped", session_id=session_id)
        elif err is not None:
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
            log.info("turn.recovery_orphan_archived", session_id=session_id)
            return
    except Exception as err:
        if _no_archive(err):
            log.info("turn.recovery_orphan_archive_skipped", session_id=session_id)
            return
        log.warning(
            "turn.recovery_orphan_archive_failed",
            session_id=session_id,
            error=str(err)[:200],
        )


def _no_archive(err: BaseException | None) -> bool:
    return isinstance(err, UnsupportedCapability) and "session_archive" in err.missing


async def _replace_dead_session(
    deps: TurnDeps,
    prepared: PreparedTurn,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    dead_session_id: str,
    dead_mapping_id: uuid.UUID,
    active_turn_message_id: str | None = None,
) -> _Replacement:
    async def attempt():
        async with deps.preparation_gate.hold(), pool_headroom(deps.sessionmaker, preparation=True):
            return await _replace_dead_session_locked(
                deps,
                prepared,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                dead_session_id=dead_session_id,
                dead_mapping_id=dead_mapping_id,
                active_turn_message_id=active_turn_message_id,
            )

    return await retry_fences(attempt)


async def _replace_dead_session_locked(
    deps: TurnDeps,
    prepared: PreparedTurn,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    dead_session_id: str,
    dead_mapping_id: uuid.UUID,
    active_turn_message_id: str | None = None,
) -> _Replacement:
    """Mark the dead mapping dead and move onto one live replacement.

    `active_turn_message_id` is the turn's own in-flight marker, when its
    adapter wrote one: it moves onto the replacement in the same locked
    transaction (`transfer_active_turn`), before the replacement's turn is
    sent, so restart recovery watches the session that is actually running.

    Runs under the same per-(tenant, platform, thread, account) advisory lock
    as `prepare_session_for_turn`. Two turns can be running on one mapping
    (a Discord wizard submit does not queue behind a mention), and both see
    the session die. Without the lock each marked the row dead and created
    its own replacement, leaving two live rows for one thread; a bind landing
    between the mark and the create did the same. Under the lock the second
    recovery finds the first one's replacement live and adopts it, and a bind
    sees either the old row or the replacement, never neither.
    """
    if prepared._preparation is not None and prepared._preparation.native_budget_usd is not None:  # pyright: ignore[reportPrivateUsage]
        from daimon.core.neutral_resources import WorkerSpecRefused

        raise WorkerSpecRefused("native_budget_usd", "unsupported")
    # A recovery can run long after admission: decide the turn again on the
    # current policy before its successor is created or adopted, and stamp any
    # seal added since (`reauthorize`).
    admission = await reauthorize(deps, prepared.admission)
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
            if dead_row is not None and (
                dead_row.status == "retired"
                or (
                    dead_row.status == "superseded"
                    and (
                        live is None
                        or live.id == dead_mapping_id
                        or live.ma_agent_id != admission.agent.id
                    )
                )
            ):
                raise SessionRetired("This conversation moved. Continue in the new one.")

            if live is not None and live.id != dead_mapping_id:
                # The lock wait above can be long: decide again before adopting
                # another turn's replacement. A pin raises `AdmissionDenied`; a
                # seal added since, or a replacement built writable that the
                # current decision makes read-only, makes this turn wait (rolls
                # back the dead-mark) so the next turn rebuilds it read-only.
                current = await reauthorize(deps, admission)
                snapshot = live.effective_config
                writable = snapshot is None or not snapshot.memory_read_only
                if current.origin_seal_ids != admission.origin_seal_ids or (
                    current.memory_read_only and writable
                ):
                    raise SessionBusyError(
                        pending_reasons=("seal",),
                        retry_after=datetime.now(UTC) + timedelta(seconds=1),
                    )
                if live.ma_agent_id is not None and live.ma_agent_id != admission.agent.id:
                    raise SessionRetired(
                        "This conversation moved to another agent. Continue there."
                    )
                admission = current
                await stamp_session_seal(
                    deps,
                    live.ma_session_id,
                    admission,
                    tenant_id=tenant_id,
                    now=lambda: datetime.now(UTC),
                )
                if active_turn_message_id is not None:
                    await transfer_active_turn(
                        db,
                        from_id=dead_mapping_id,
                        to_id=live.id,
                        expected_message_id=active_turn_message_id,
                    )
                return _Replacement(
                    ma_session_id=live.ma_session_id,
                    mapping_id=live.id,
                    model_id=(
                        snapshot.pricing_id
                        if snapshot is not None
                        else model_choice_id(admission.agent.model)
                    ),
                    transfer_kind=live.transfer_kind or "history",
                    previous_session=None,
                    adopted=True,
                    admission=admission,
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
                io=turn_io(
                    deps.anthropic,
                    dead_session_id,
                    **_turn_port_kwargs(deps, admission, dead_session_id, tenant_id=tenant_id),
                ),
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
            created = await create_ma_session(
                deps,
                admission,
                tenant_id=tenant_id,
                predecessor_session_id=dead_session_id,
                platform=platform,
            )
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
            await mark_dead(db, id=dead_mapping_id)
            await link_replacement(db, id=dead_mapping_id, replaced_by_id=fresh.mapping_id)
            if active_turn_message_id is not None:
                await transfer_active_turn(
                    db,
                    from_id=dead_mapping_id,
                    to_id=fresh.mapping_id,
                    expected_message_id=active_turn_message_id,
                )
    except BaseException:
        # The locked transaction rolled back after the upstream session was
        # created (a ceiling, a cancel, a failed insert or commit). No row
        # names that session any more, so nothing else would ever archive it.
        if created is not None:
            try:
                await _archive_orphaned_session(
                    deps.anthropic,
                    session_id=created.ma_session_id,
                    io=turn_io(
                        deps.anthropic,
                        created.ma_session_id,
                        **_turn_port_kwargs(
                            deps, admission, created.ma_session_id, tenant_id=tenant_id
                        ),
                    ),
                )
            except Exception as cleanup_error:
                # Binding validation can fail before the archive task exists.
                # Preserve the recovery error while denying unauthorized I/O.
                log.warning(
                    "turn.recovery_orphan_archive_failed",
                    session_id=created.ma_session_id,
                    error=str(cleanup_error)[:200],
                )
        raise

    return _Replacement(
        ma_session_id=fresh.ma_session_id,
        mapping_id=fresh.mapping_id,
        model_id=fresh.snapshot.pricing_id,
        transfer_kind=loss_transfer_kind,
        previous_session=previous_session,
        adopted=False,
        admission=created.admission or admission,
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
    origin: TurnContext = "chat",
    context_mention_ids: tuple[str, ...] = (),
    context_message_text: str | None = None,
    image_blocks: Sequence[BetaManagedAgentsImageBlockParam] | None = None,
    render_interval_s: float = 2.0,
    deadline: datetime | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    confirm_write: ConfirmationHook | None = None,
    provider_action: ProviderActionHook | None = None,
    attended: bool | None = None,
    operation_key: str | None = None,
    active_turn_message_id: str | None = None,
    before_send: Callable[[], Awaitable[None]] | None = None,
    turn_card_intent_id: uuid.UUID | None = None,
    worker_finished: TaskContinuationRow | None = None,
    watch_row: TaskContinuationRow | None = None,
    job_row: TaskContinuationRow | None = None,
) -> RunOutcome:
    """Drive an admitted turn, including fleet workers and unattended wakes.

    Unattended Managed Agents sends re-gate the current session before each
    message and confirmation batch. Runtimes without a session read refuse
    unattended confirmations with a plain reason, including with safety off.
    """
    observation = (
        prepared.admission.observation
        or current_outcome.get()
        or TurnObservation(deps.sessionmaker, tenant_id, platform, thread_id=thread_id)
    )
    observation.thread_id = thread_id
    observation.account_id = prepared.admission.account_id
    observation.agent_id = prepared.admission.agent.id
    observation.session_id = prepared.ma_session_id
    observation.origin = origin
    observation.backend_profile = (
        prepared.admission.backend_footer_profile
        if prepared.admission.backend_footer_profile is not None and deps.backend_selection_enabled
        else None
    )
    try:
        with observation.activate(), deps.running_turns.track():
            result = await run_prepared_turn_impl(
                deps,
                prepared,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                external_user_id=external_user_id,
                user_message=user_message,
                lifecycle=lifecycle,
                cancel=cancel,
                reseed_user_message=reseed_user_message,
                recovery_lifecycle=recovery_lifecycle,
                origin=origin,
                context_mention_ids=context_mention_ids if platform == "discord" else (),
                context_message_text=context_message_text,
                image_blocks=image_blocks,
                render_interval_s=render_interval_s,
                deadline=deadline,
                now=now,
                confirm_write=confirm_write,
                provider_action=provider_action,
                attended=attended,
                operation_key=operation_key,
                active_turn_message_id=active_turn_message_id,
                before_send=before_send,
                turn_card_intent_id=turn_card_intent_id,
                worker_finished=worker_finished,
                watch_row=watch_row,
                job_row=job_row,
            )
    except BaseException as exc:
        observation.finish(error=exc)
        raise
    finally:
        finalize = prepared._finalize  # pyright: ignore[reportPrivateUsage]
        if finalize is not None:
            # Credential revocation must survive a second cancel of this task:
            # the shielded call keeps running even if this wait is cancelled.
            pending = asyncio.ensure_future(finalize())
            _PENDING_FINALIZERS.add(pending)
            pending.add_done_callback(_finalizer_done)
            try:
                await asyncio.wait_for(asyncio.shield(pending), timeout=_FINALIZE_TIMEOUT_S)
            except TimeoutError:
                log.warning("turn.finalize_timeout", timeout_s=_FINALIZE_TIMEOUT_S)
    observation.session_id = result.ma_session_id
    observation.finish(state=result.state, recovered=result.recovered)
    return result


#: Origins whose lifecycle draws the turn in a chat for a person to read.
_CHAT_RENDERED_ORIGINS: frozenset[TurnContext] = frozenset({"chat", "handoff"})


async def run_prepared_turn_impl(
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
    origin: TurnContext = "chat",
    context_mention_ids: tuple[str, ...] = (),
    context_message_text: str | None = None,
    image_blocks: Sequence[BetaManagedAgentsImageBlockParam] | None = None,
    render_interval_s: float = 2.0,
    deadline: datetime | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    confirm_write: ConfirmationHook | None = None,
    provider_action: ProviderActionHook | None = None,
    attended: bool | None = None,
    operation_key: str | None = None,
    active_turn_message_id: str | None = None,
    before_send: Callable[[], Awaitable[None]] | None = None,
    turn_card_intent_id: uuid.UUID | None = None,
    worker_finished: TaskContinuationRow | None = None,
    watch_row: TaskContinuationRow | None = None,
    job_row: TaskContinuationRow | None = None,
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

    `confirm_write` is the adapter's confirmation-card hook and `attended`
    says whether a person is waiting (default: every origin but `routine`).
    Unattended turns follow `daimon.core.turn.unattended`, recovery included:
    a gate failure prevents the batch from being sent, and a Managed Agents
    backend without a session read is refused with `AdmissionDenied`.
    Attended writes use the adapter's card hook when the policy requires it;
    a missing hook refuses them with `no_confirmation_surface`.

    `active_turn_message_id` is the in-flight marker the adapter wrote on
    `prepared.mapping_id` for this turn, if any. A recovery moves it onto the
    replacement mapping before the re-run is sent, so a restart mid-recovery
    leaves the marker on the session that is actually running.

    `deadline`/`now` bound the WHOLE body -- first attempt, recovery setup,
    and the recovery re-run -- against the per-turn ceiling.
    `deadline=None` is fail-safe, not off: it computes
    `turn_deadline(now=now())` so a caller that never passes a deadline is
    still ceiling-covered. On breach: the mapping id the FINAL attempt was
    running against (tracked as it moves through recovery) is marked dead
    by the prepared-turn boundary so the next
    mention does not bind the same wedged session, the caller's own
    `lifecycle.on_terminal_failure` is invoked directly (the driver's
    finalizers never ran, so nothing else would render this), and a
    `RunOutcome` carrying `state.error.kind == "ceiling"` is returned rather
    than raised -- adapters take their existing `state.error is not None`
    branch with no new code. This can never be misread as a dead-session
    signal (`_is_dead_session` gates on `kind == "upstream"`), so a ceiling
    breach can never loop into a second wall-clock-priced attempt.
    """
    if origin in _CHAT_RENDERED_ORIGINS:
        lifecycle, recovery_lifecycle = scrubbed_for(platform, lifecycle, recovery_lifecycle)

    if job_row is not None and (job_row.reason != "job_finished" or attended is not False):
        raise ValueError("Job completion turns must be explicitly unattended")
    if job_row is not None and (
        job_row.tenant_id != tenant_id
        or job_row.platform != platform
        or job_row.thread_id != thread_id
        or job_row.requester_account_id != prepared.admission.account_id
        or job_row.target_ma_agent_id != prepared.admission.agent.id
    ):
        raise AdmissionDenied(reason="invoker_not_allowed")
    if watch_row is not None and (watch_row.reason != "watch_fired" or attended is not False):
        raise ValueError("Watch turns must be explicitly unattended")
    effective_deadline = deadline if deadline is not None else turn_deadline(now=now())
    unattended = not (attended if attended is not None else origin != "routine")
    # Attendance, rather than a list of wake reasons, distinguishes a current
    # human invocation from work running for a recorded owner.
    if unattended and prepared.admission.grant is not None:
        automated_grant = replace(prepared.admission.grant, human_invocation=False)
        automated = replace(prepared.admission, grant=automated_grant)
        # Check authority without claiming the prepared session has adopted new
        # seals or mounts. The send gate must still detect posture changes and
        # require preparation again before it sends into that session.
        await reauthorize(deps, automated)
        prepared = replace(prepared, admission=automated)
    # Tracks the session/mapping id (and whether recovery has taken over) the
    # FINAL attempt is running against, updated by `_run` the instant recovery
    # recreates -- so a ceiling breach mid-recovery marks the NEW mapping dead,
    # not the stale one already marked dead by the ordinary recovery cycle.
    active_session_id_cell: list[str] = [prepared.ma_session_id]
    active_mapping_id_cell: list[uuid.UUID | None] = [prepared.mapping_id]
    recovered_cell: list[bool] = [False]
    continuity_cell: list[ContinuityOutcome] = [prepared.continuity]
    # The recorder bound to the session in `active_session_id_cell`, for the
    # turn-end reconcile: after recovery it is the replacement's, priced at
    # the replacement's model.
    active_record_cell: list[UsageRecorder | ObservationRecorder] = [prepared._record]  # pyright: ignore[reportPrivateUsage]

    active_admission_cell: list[Admission] = [prepared.admission]
    # The mux persistence of the attempt now running; while it waits for a
    # thread lease held by another turn, this turn runs nothing on its mapping.
    active_persistence_cell: list[TurnPersistence | None] = [None]

    # A replacement session's first user message opens with daimon's framing
    # for the work it inherited (`daimon.core.handoff_context`). Empty for
    # every ordinary turn, which then sends byte-identical bytes to before.
    context = context_prompt(origin, system=prepared.admission.agent.system)
    plans = await progress_plans_prefix(
        deps.sessionmaker,
        deployment=deps.progress_plans,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=prepared.admission.origin_channel_id,
        thread_id=thread_id,
        chat_rendered=origin in _CHAT_RENDERED_ORIGINS,
    )
    prefix = context + plans + prepared.continuity.user_prefix
    if prepared.admission.model_fell_back:
        prefix += MODEL_FALLBACK_NOTICE

    async def load_context_config(request: ContextRequest) -> LoadedProvider | None:
        from daimon.core.stores import context_providers
        from daimon.core.stores.tenants import get_tenant

        async with deps.sessionmaker.begin() as session:
            config = await context_providers.load(
                session,
                tenant_id=request.tenant_id,
                channel_id=request.parent_channel_id or request.channel_id,
                agent_id=request.agent_id,
            )
            if config is None or not config.enabled:
                return None
            tenant = await get_tenant(session, request.tenant_id)
            if tenant is None or tenant.archived_at is not None:
                return None
            return LoadedProvider(config, tenant.external_id)

    def context_hook_for(admission: Admission, model_id: str | None) -> ContextHook | None:
        revision = admission.backend_revision
        if revision is not None and revision.profile != MANAGED_AGENTS.profile_id:
            return None
        grant = admission.grant
        # No fabricated placement from message text or budget attribution.
        if grant is None or grant.tenant_id != tenant_id:
            return None
        place = grant.run_place
        if place.parent_unresolved or (place.channel_id is None and not grant.is_dm):
            return None
        is_thread = bool(place.parent_channel_id and place.parent_channel_id != place.channel_id)
        return ContextHook(
            deps.context_providers,
            ContextRequest(
                tenant_id=tenant_id,
                server_id="",
                platform=platform,
                channel_id=(admission.private_dm_id or grant.channel_id)
                if grant.is_dm
                else place.channel_id,
                parent_channel_id=place.parent_channel_id if is_thread else None,
                thread_id=admission.origin_thread_id,
                place_type="dm" if grant.is_dm else "thread" if is_thread else "channel",
                requester_platform_user_id=external_user_id,
                agent_id=admission.agent.id,
                mention_ids=context_mention_ids if platform == "discord" else (),
                message_text=(context_message_text or "") if platform == "discord" else "",
            ),
            load_context_config,
            system_message_supported=supports_system_message(model_id or admission.agent.model.id),
        )

    async def authorize_watch(admission: Admission, session_id: str) -> None:
        assert watch_row is not None
        from daimon.core.continuity.watch_turn import require_watch_send

        await require_watch_send(
            deps.sessionmaker,
            watch_row,
            admission,
            session_id=session_id,
            tenant_id=tenant_id,
            platform=platform,
            thread_id=thread_id,
        )
        await decide_before_send(deps, admission, attended=not unattended)()
        await require_current_spend(
            deps,
            tenant_id=watch_row.tenant_id,
            platform=watch_row.platform,
            external_user_id=watch_row.requester_external_user_id,
            budget_channel_id=admission.budget_channel_id or watch_row.parent_channel_id,
            now=now(),
        )
        if before_send is not None:
            await before_send()

    @contextlib.asynccontextmanager
    async def persisted(
        admission: Admission,
        session_id: str,
        mapping_id: uuid.UUID | None,
        session_ref: ResourceRef | None = None,
        *,
        attempt_cancel: asyncio.Event = cancel,
        confirmation: ToolConfirmation,
        backend: ManagedAgents | None = None,
        scope: Scope | None = None,
    ) -> AsyncIterator[ToolConfirmation]:
        origin_id = current_origin_id.get() if origin != "worker" else None

        @contextlib.asynccontextmanager
        async def origin_grant(lease: Lease | None = None) -> AsyncIterator[None]:
            with execution_origin(origin_id):
                if origin_id is None:
                    yield
                    return
                try:
                    async with deps.sessionmaker.begin() as db:
                        mapping = (
                            await get_thread_session_by_id(db, id=mapping_id)
                            if mapping_id is not None
                            else None
                        )
                        grant = (
                            mapping.effective_config.chat_session_id
                            if mapping is not None
                            and mapping.ma_session_id == session_id
                            and mapping.tenant_id == tenant_id
                            and mapping.account_id == admission.account_id
                            and mapping.effective_config is not None
                            else None
                        )
                        if (
                            mapping_id is None
                            and session_id == prepared.ma_session_id
                            and admission == prepared.admission
                            and prepared.session_ref is not None
                            and prepared.session_ref.tenant_id == str(tenant_id)
                            and prepared.session_ref.account_id == str(admission.account_id)
                        ):
                            grant = prepared.native_chat_session_id
                        await bind_origin_session(
                            db,
                            origin_id=origin_id,
                            chat_session_id=grant,
                            attended=not unattended,
                            lease=lease,
                        )
                    yield
                finally:
                    # Clear only this origin. If its lease was lost and a successor
                    # already bound the grant, never revoke the successor's authority.
                    async def unbind() -> None:
                        assert origin_id is not None
                        async with deps.sessionmaker.begin() as db:
                            await bind_origin_session(db, origin_id=origin_id, chat_session_id=None)

                    await complete(unbind())

        # Preparation/queueing cannot borrow an ambient parent execution.
        with execution_origin(None):
            selected_path = deps.turn_path or load_turn_settings().path
            posture = confirmation
            async with contextlib.AsyncExitStack() as stack:
                if unattended:
                    revision = admission.backend_revision
                    managed = revision is None or revision.profile == MANAGED_AGENTS.profile_id
                    gateable = managed
                    read_session: Callable[[], Awaitable[BetaManagedAgentsSession]] | None = None
                    injected = (backend or deps.backend) if selected_path == "mux" else None
                    gate_scope = admitted_session_scope(
                        admission, tenant_id=tenant_id, session_id=session_id, prepared=scope
                    )
                    if gateable and injected is not None:
                        # An injected mux runtime must offer the actual snapshot
                        # read; its profile alone does not prove it can re-gate.
                        ref = session_ref
                        if ref is None and deps.backend_session_ref is not None:
                            ref = deps.backend_session_ref(session_id, gate_scope)
                        try:
                            reads = injected.extension(
                                SessionReads, namespace=SESSION_READS_NAMESPACE, version=1
                            )
                        except UnsupportedCapability:
                            gateable = False
                        else:
                            if ref is None:
                                gateable = False
                            else:

                                async def read_injected_session() -> BetaManagedAgentsSession:
                                    return BetaManagedAgentsSession.model_validate(
                                        await legacy_call(reads.read_native(gate_scope, ref))
                                    )

                                read_session = read_injected_session

                    if (managed or watch_row is not None or job_row is not None) and not gateable:
                        # An unreadable unattended session cannot be constrained.
                        raise AdmissionDenied(reason="backend_unsupported")
                    if (
                        job_row is None
                        and watch_row is None
                        and admission.grant is not None
                        and admission.grant.is_dm
                    ):
                        if not gateable:
                            raise AdmissionDenied(reason="backend_unsupported")
                        posture, dm_gate = await dm_session_authority(
                            deps,
                            admission,
                            session_id,
                            tenant_id=tenant_id,
                            read_session=read_session,
                            entry_point=unattended_entry_point(origin),
                        )
                        stack.enter_context(unattended_sends(dm_gate))
                    elif gateable:
                        if watch_row is None and job_row is None:
                            gate = session_gate(
                                deps.anthropic,
                                session_id,
                                deps.tool_safety,
                                scope=gate_scope,
                                public_url=deps.public_url,
                                session_factory=deps.sessionmaker,
                                tenant_id=tenant_id,
                                read_session=read_session,
                                entry_point=unattended_entry_point(origin),
                            )
                        else:
                            from daimon.core.continuity.watch_turn import watch_capabilities
                            from daimon.core.session_ports_compat import retrieve_session_record

                            original_snapshot: BetaManagedAgentsSession | None = None

                            async def _fenced_wake_gate() -> None:
                                nonlocal original_snapshot
                                if job_row is None:
                                    await authorize_watch(admission, session_id)
                                snapshot = (
                                    await read_session()
                                    if read_session is not None
                                    else await retrieve_session_record(
                                        deps.anthropic.with_options(max_retries=0),
                                        session_id,
                                        scope=gate_scope,
                                    )
                                )
                                row = job_row if job_row is not None else watch_row
                                assert row is not None
                                if (
                                    snapshot.id != session_id
                                    or snapshot.agent.id != row.target_ma_agent_id
                                ):
                                    raise AdmissionDenied(reason="invoker_not_allowed")
                                if job_row is not None:
                                    constrained = await constrain_capabilities(
                                        snapshot,
                                        deps.tool_safety,
                                        public_url=deps.public_url,
                                        session_factory=deps.sessionmaker,
                                        tenant_id=tenant_id,
                                        allow_builtins=capability_policy("wake").allow_builtins,
                                        allow_skills=capability_policy("wake").allow_skills,
                                        tool_allowlist=_JOB_ALLOWLIST,
                                    )
                                else:
                                    constrained = await watch_capabilities(
                                        snapshot,
                                        deps.tool_safety,
                                        public_url=deps.public_url,
                                        sessions=deps.sessionmaker,
                                        tenant_id=tenant_id,
                                    )
                                if original_snapshot is None and constrained.changed:
                                    await invalidate_cached_tools(
                                        snapshot, constrained, session_factory=deps.sessionmaker
                                    )
                                    original_snapshot = snapshot
                                    stack.push_async_callback(
                                        restore_capabilities,
                                        deps.anthropic.with_options(max_retries=0, timeout=30),
                                        original_snapshot,
                                        constrained.original,
                                        scope=gate_scope,
                                    )
                                await send_capabilities(
                                    deps.anthropic.with_options(max_retries=0),
                                    snapshot,
                                    constrained,
                                    scope=gate_scope,
                                )

                            gate = _fenced_wake_gate
                        stack.enter_context(unattended_sends(gate))
                    else:
                        # Selfhosted pauses every declared tool at the host boundary,
                        # including automatic tools when unattended. Keep the composed
                        # read/write and sandbox decider for those inspected actions.
                        if revision is None or revision.profile != SELFHOSTED_LOOP.profile_id:
                            posture = PolicyApproval(decide=refuse_ungated_confirmation)
                            require_native_unattended_capabilities(
                                prepared._native_spec,  # pyright: ignore[reportPrivateUsage]
                                profile_id=revision.profile if revision is not None else "unknown",
                            )

                            async def native_gate() -> None:
                                require_native_unattended_capabilities(
                                    prepared._native_spec,  # pyright: ignore[reportPrivateUsage]
                                    profile_id=revision.profile
                                    if revision is not None
                                    else "unknown",
                                )

                            stack.enter_context(unattended_sends(native_gate))
                        else:
                            stack.enter_context(unattended_sends(None))
                if selected_path == "legacy":
                    async with origin_grant():
                        yield posture
                    return
                persistence = await prepared_persistence(
                    deps,
                    admission,
                    tenant_id=tenant_id,
                    platform=platform,
                    thread_id=thread_id,
                    session_id=session_id,
                    mapping_id=mapping_id,
                    operation_key=operation_key,
                    session_ref=session_ref,
                    scope=prepared._scope,  # pyright: ignore[reportPrivateUsage]
                    fleet_capture=deps.fleet_enabled and prepared._source is not None,  # pyright: ignore[reportPrivateUsage]
                    execution_context=origin_grant,
                    lease_wait=LeaseWait(
                        timeout_s=remaining_s(effective_deadline, now=now()),
                        cancel=attempt_cancel,
                    ),
                )
                active_persistence_cell[0] = persistence
                with persistence.activate():
                    yield posture

    root_intent = turn_card_intent_id if deps.fleet_enabled and origin != "worker" else None

    async def checked_send() -> None:
        await decide_before_send(deps, prepared.admission, attended=not unattended)()
        if worker_finished is not None:
            from daimon.core.neutral_resources import WorkerSpecRefused
            from daimon.core.turn.runtime_manifest import resume_prepared_root

            if root_intent is None:
                raise WorkerSpecRefused("origin", "unbound")
            await resume_prepared_root(
                deps, prepared, worker_finished, intent_id=root_intent, now=now
            )
        elif root_intent is not None:
            from daimon.core.turn.runtime_manifest import freeze_prepared_root

            await freeze_prepared_root(deps, prepared, intent_id=root_intent, now=now)
        if before_send is not None:
            await before_send()

    async def _run() -> RunOutcome:
        confirmed_tools: frozenset[str] = await confirmed_tools_for_tenant(deps, tenant_id)
        if confirmed_tools:
            revision = prepared.admission.backend_revision
            if (
                revision is not None
                and get_profile(revision.profile).support_for("tool_confirmation") not in SUPPORTED
            ):
                confirmed_tools = frozenset()
        server_urls = {
            s.name: s.url
            for s in session_mcp_servers(
                prepared.admission.agent,
                frozenset(),
                tool_safety=deps.tool_safety,
                public_url=deps.public_url,
                confirmed_tools=confirmed_tools,
            )
        }
        trusted_servers = trusted_servers_for(deps.public_url, server_urls)
        prepared_source = prepared._source  # pyright: ignore[reportPrivateUsage]
        annotations_for = (
            mcp_annotation_lookup(
                server_urls={
                    s.name: s.url
                    for s in prepared.admission.agent.mcp_servers
                    if s.name in deps.tool_safety.trusted_annotations
                },
                token_for=agent_mcp_token_for(
                    deps.sessionmaker,
                    fernet=deps.fernet,
                    tenant_id=tenant_id,
                    agent_id=(
                        prepared_source.agent_id
                        if prepared_source is not None
                        else derive_agent_uuid(
                            tenant_id=tenant_id, ma_agent_id=str(prepared.admission.agent.id)
                        )
                    ),
                ),
            )
            # Only servers the operator trusts for annotations are read; with
            # none trusted no catalogue is ever listed.
            if deps.tool_safety.enabled and deps.tool_safety.trusted_annotations
            else None
        )
        rules_channel = channel_for_tool_rules(
            platform=platform,
            channel_id=prepared.admission.origin_channel_id,
            private_dm_id=prepared.admission.private_dm_id,
        )
        rule_lookup = agent_rule_lookup(
            deps.sessionmaker,
            tenant_id=tenant_id,
            agent_id=str(prepared.admission.agent.id),
            server_urls={s.name: s.url for s in prepared.admission.agent.mcp_servers},
            also_urls=daimon_endpoint(deps.public_url),
            channel_id=rules_channel,
        )
        if unattended:
            # A fleet worker or a wake nobody asked for just now: the one
            # unattended decider, policy on or off, with Daimon's card-only actions
            # refused, never the chat posture's safety-off shortcuts.
            tool_confirmation = PolicyApproval(
                decide=unattended_decider(
                    deps.tool_safety,
                    trusted_servers=trusted_servers,
                    confirmed_tools=confirmed_tools | protected_tools(deps.tool_safety),
                    daimon_rules=True,
                    annotations_for=annotations_for,
                    rules_for=rule_lookup,
                )
            )

            tool_confirmation = capability_posture(
                tool_confirmation, tool_allowlist=_JOB_ALLOWLIST if job_row is not None else None
            )
        else:
            tool_confirmation = chat_tool_confirmation(
                deps.tool_safety,
                provider_permissions=(
                    prepared.admission.backend_revision is not None
                    and prepared.admission.backend_revision.profile == SELFHOSTED_LOOP.profile_id
                ),
                requester_platform_user_id=external_user_id,
                confirm=confirm_write,
                trusted_servers=trusted_servers,
                asks_before_publishing=prepared.admission.asks_before_publishing,
                approval_gate_for=lambda: refresh_approval_gate(deps, prepared.admission),
                server_urls=server_urls,
                confirmed_tools=confirmed_tools,
                daimon_rules=bool(prepared.admission.daimon_rule_tools),
                rules_for=(
                    rule_lookup
                    if deps.tool_safety.enabled and deps.tool_safety.agent_rules
                    else None
                ),
                rule_card_for=(
                    lambda call: (
                        (
                            ToolRuleCardReference(
                                tenant_id=tenant_id,
                                agent_id=str(prepared.admission.agent.id),
                                agent_name=prepared.admission.agent.name,
                                tool_key=call.key,
                                server_url=url,
                                channel_id=rules_channel,
                            )
                            if (
                                url := next(
                                    (
                                        server.url
                                        for server in prepared.admission.agent.mcp_servers
                                        if server.name == call.server_name
                                    ),
                                    None,
                                )
                            )
                            is not None
                            else None
                        )
                        if deps.tool_safety.enabled and deps.tool_safety.agent_rules
                        else None
                    )
                ),
                annotations_for=annotations_for,
            )

        if watch_row is not None:
            from daimon.core.continuity.watch_turn import watch_posture

            tool_confirmation = watch_posture(
                deps.tool_safety,
                rules_for=agent_rule_lookup(
                    deps.sessionmaker,
                    tenant_id=tenant_id,
                    agent_id=str(prepared.admission.agent.id),
                    server_urls={s.name: s.url for s in prepared.admission.agent.mcp_servers},
                    channel_id=channel_for_tool_rules(
                        platform=platform,
                        channel_id=prepared.admission.origin_channel_id,
                        private_dm_id=prepared.admission.private_dm_id,
                    ),
                ),
            )

        ma_session_id = prepared.ma_session_id
        mapping_id = prepared.mapping_id

        action_kwargs: _ProviderActionKwargs = {}
        if (
            not unattended
            and provider_action is not None
            and prepared.admission.backend_revision is not None
            and prepared.admission.backend_revision.profile != "anthropic.managed_agents"
        ):
            action_kwargs["provider_actions"] = ProviderActionApproval(
                requester=ProviderActionRequester(platform, thread_id, external_user_id),
                deadline=effective_deadline,
                timeout=timedelta(seconds=deps.tool_safety.confirmation_timeout_s),
                cancel=cancel,
                hook=provider_action,
                now=now,
            )

        async def check_watch_delivery() -> None:
            assert watch_row is not None
            try:
                await authorize_watch(prepared.admission, ma_session_id)
            except asyncio.CancelledError:
                raise
            except AdmissionDenied:
                cancel.set()
                raise

        delivery = OutputAuthorization(check_watch_delivery) if watch_row is not None else None
        if delivery is not None:
            install = getattr(lifecycle, "set_output_authorization", None)
            if install is not None:
                cast(Callable[[OutputAuthorization], None], install)(delivery)
        first_attempt = _DeferredFailureLifecycle(
            inner=lifecycle, authorize_final=delivery.check if delivery is not None else None
        )
        superseded: BindingSuperseded | None = None
        try:
            async with persisted(
                prepared.admission,
                ma_session_id,
                mapping_id,
                prepared.session_ref,
                confirmation=tool_confirmation,
                backend=prepared.backend,
                scope=prepared._scope,  # pyright: ignore[reportPrivateUsage]
            ) as attempt_confirmation:
                with output_authorization(delivery):
                    try:
                        state = await run_turn(
                            **_turn_port_kwargs(
                                deps,
                                prepared.admission,
                                ma_session_id,
                                tenant_id=tenant_id,
                                provider_backend=prepared.backend,
                                provider_session=prepared.session_ref,
                                provider_runtime=prepared.runtime,
                                provider_scope=prepared._scope,  # pyright: ignore[reportPrivateUsage]
                            ),
                            anthropic=deps.anthropic,
                            session_id=ma_session_id,
                            user_message=_with_prefix(prefix, user_message),
                            lifecycle=first_attempt,
                            cancel=cancel,
                            render_interval_s=render_interval_s,
                            billing=prepared_billing(prepared, tenant_id=tenant_id),
                            tool_confirmation=attempt_confirmation,
                            **action_kwargs,
                            image_blocks=image_blocks,
                            system_blocks=prepared.continuity.system_blocks,
                            context_hook=context_hook_for(
                                prepared.admission, prepared.session_model_id
                            ),
                            before_send=checked_send
                            if before_send is not None
                            or root_intent is not None
                            or worker_finished is not None
                            else decide_before_send(
                                deps, prepared.admission, attended=not unattended
                            ),
                            send_guard=lambda: session_mutation_fence(
                                deps.sessionmaker, ma_session_id
                            ),
                        )
                        if delivery is not None:
                            await first_attempt.flush_held_failure()
                    finally:
                        if delivery is not None:
                            delivery.close()
        except BindingSuperseded as error:
            superseded = error
            state = TurnState(error=TurnError(kind="upstream", message=str(error)))

        if not (
            (superseded is not None or _is_dead_session(state))
            and before_send is None
            and worker_finished is None
            and watch_row is None
            and mapping_id is not None
            and (
                prepared.admission.backend_revision is None
                or prepared.admission.backend_revision.profile == "anthropic.managed_agents"
            )
        ):
            if superseded is not None:
                raise superseded
            await first_attempt.flush_held_failure()
            return RunOutcome(
                state=state,
                ma_session_id=ma_session_id,
                mapping_id=mapping_id,
                recovered=False,
                continuity=prepared.continuity,
            )

        # A cancel already signalled by the time we'd start recovery
        # means the user asked to stop before we ever attempted a second full
        # agentic turn -- running one anyway would bill work nobody wanted.
        # Abort recovery and surface the withheld first-attempt failure as
        # the final outcome, same shape as the non-recoverable branch above.
        if cancel.is_set():
            await first_attempt.flush_held_failure(TerminationReason.RECOVERY_CANCELLED)
            return RunOutcome(
                state=replace(state, termination=TerminationReason.RECOVERY_CANCELLED),
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
                active_turn_message_id=active_turn_message_id,
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
                channel_id=(recovery.admission or prepared.admission).budget_channel_id,
            )

            active_record_cell[0] = new_record
            active_admission_cell[0] = recovery.admission or prepared.admission

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
            reseeded_message = _with_prefix(plans + loss_user_prefix, await reseed_user_message())
            fresh_cancel = asyncio.Event()
            new_lifecycle = recovery_lifecycle(fresh_cancel)
            # Mirror a LATE cancel on the ORIGINAL event into
            # `fresh_cancel` for the duration of the recovery turn -- see
            # `_mirror_cancel`'s docstring for the window this closes.
            mirror_task = asyncio.create_task(
                _mirror_cancel(cancel, fresh_cancel), name="turn.cancel_mirror"
            )
            try:
                async with persisted(
                    recovery.admission or prepared.admission,
                    new_session_id,
                    new_mapping_id,
                    attempt_cancel=fresh_cancel,
                    confirmation=tool_confirmation,
                ) as recovery_confirmation:
                    recovered_state = await run_turn(
                        **_turn_port_kwargs(
                            deps,
                            recovery.admission or prepared.admission,
                            new_session_id,
                            tenant_id=tenant_id,
                        ),
                        anthropic=deps.anthropic,
                        session_id=new_session_id,
                        user_message=context + reseeded_message,
                        lifecycle=new_lifecycle,
                        cancel=fresh_cancel,
                        render_interval_s=render_interval_s,
                        billing=Billed(record=new_record),
                        tool_confirmation=recovery_confirmation,
                        image_blocks=image_blocks,
                        system_blocks=loss_system_blocks,
                        context_hook=context_hook_for(
                            recovery.admission or prepared.admission, recovery.model_id
                        ),
                        # Reseeding and opening the stream await platform and MA
                        # calls after recovery's last decision.
                        before_send=decide_before_send(
                            deps, recovery.admission or prepared.admission, attended=not unattended
                        ),
                        send_guard=lambda: session_mutation_fence(
                            deps.sessionmaker, recovery.ma_session_id
                        ),
                    )
            finally:
                if not mirror_task.done():
                    mirror_task.cancel()
                    with contextlib.suppress(BaseException):
                        await mirror_task
        except Exception as exc:
            if (observation := current_outcome.get()) is not None:
                observation.finish(error=exc, reason=TerminationReason.RECOVERY_FAILED)
            await first_attempt.flush_held_failure(TerminationReason.RECOVERY_FAILED)
            raise

        return RunOutcome(
            state=recovered_state,
            ma_session_id=new_session_id,
            mapping_id=new_mapping_id,
            recovered=True,
            continuity=continuity_cell[0],
        )

    async def _run_within_ceiling() -> RunOutcome:
        try:
            return await asyncio.wait_for(
                _run(), timeout=remaining_s(effective_deadline, now=now())
            )
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

            persistence = active_persistence_cell[0]
            queued = persistence is not None and not persistence.lease_acquired
            if active_mapping_id is not None and not queued:
                # A turn still queued behind another never ran on this mapping,
                # and its holder is using it: only a turn that ran retires it.
                async with deps.sessionmaker() as session:
                    await mark_dead(session, id=active_mapping_id)
                    await session.commit()

            ceiling_state = TurnState(error=err, termination=TerminationReason.CEILING)
            try:
                await lifecycle.on_terminal_failure(ceiling_state, err)
            except Exception as render_err:
                # Rendering is delivery, not correctness -- a broken adapter hook
                # must not mask the ceiling error itself.
                log.warning("turn.ceiling_render_failed", error=str(render_err))

            return RunOutcome(
                state=ceiling_state,
                ma_session_id=active_session_id,
                mapping_id=active_mapping_id,
                recovered=recovered,
                continuity=continuity_cell[0],
            )

    # Set for the whole turn, so anything inside it (a card posted mid-turn, in this
    # task or one it starts) reads the deadline and never promises a time past it.
    deadline_token = current_turn_deadline.set(effective_deadline)
    try:
        outcome = await _run_within_ceiling()
    finally:
        current_turn_deadline.reset(deadline_token)
    # Turn end: anything this session billed nowhere -- calls MA made while no
    # worker was streaming it (an earlier turn whose worker died), while no
    # stream generation was attached, or before a ceiling cut the turn off.
    active_admission = active_admission_cell[0]
    revision = active_admission.backend_revision
    persistence = active_persistence_cell[0]
    # A turn that queued for the lease and ended (Stop, ceiling) without taking
    # it is not the session's holder: usage the holder's stream has yet to
    # record is the holder's, and recording it here would charge this user.
    queued_only = persistence is not None and not persistence.lease_acquired
    if not queued_only and (revision is None or revision.profile == "anthropic.managed_agents"):
        await _settle(
            deps,
            session_id=active_session_id_cell[0],
            scope=admitted_session_scope(
                active_admission, tenant_id=tenant_id, session_id=active_session_id_cell[0]
            ),
            record=cast(UsageRecorder, active_record_cell[0]),
        )
    return outcome
