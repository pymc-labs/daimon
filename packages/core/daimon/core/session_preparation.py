"""Make this caller's session run the configuration they asked for, then bind it.

`bind_session` used to reuse a live `thread_sessions` row verbatim. A session
freezes its agent at creation time, so "verbatim" meant a key added a minute
ago, a model changed this morning and an agent handed the task over yesterday
all reached the caller only when their session happened to be recreated. This
module closes that: at every bind it compares what the session runs against
what the caller's configuration now wants, applies what can be applied in
place, and replaces the session when it cannot.

Preparation outcomes:

- `PreparedTurn` — run the turn. `continuity` says what happened to the session.
- `PreparationDeferred` — a turn is already running, or MA refused a mid-turn
  update. The turn runs on the current session and the change lands next time.
- `PreparationBusy` — same timing, but the change is a responder switch, and
  the current session belongs to the responder being switched away from. No
  turn runs; the switch is made at the caller's next message.
- `PreparationFailure` — the change could not be made. The old session is
  untouched and still live, so nothing the caller saved is lost; the turn is
  simply not run.

Changes acquire a transaction-scoped Postgres advisory lock on the caller's
(tenant, platform, thread, account) tuple, so racing mentions create one
successor. Contenders use try-locks and release transactions and permits before
retrying each acquisition up to the existing turn ceiling; timeout raises
retryable SessionBusyError. In-process gates queue without an acquisition timer.
A compatible reused session releases that transaction before vault network I/O
and verifies that the mapping is still current afterward.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import anthropic as anthropic_pkg
import structlog
from daimon.core.authz import build_agent_ref
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.session_compat import (
    DEFAULT_MA_CAPABILITIES,
    ChangeReason,
    MaCapabilities,
    RemirrorVaultCredentials,
    ReplaceSession,
    ReuseAsIs,
    UpdateInPlace,
    UpdateOp,
    decide_session_compatibility,
)
from daimon.core.session_fence_retry import retry_fences
from daimon.core.session_mutation import lock_session_mutation
from daimon.core.session_ports_compat import (
    archive_session_record,
    retrieve_session_record,
    session_scope,
)
from daimon.core.session_preparation_gate import pool_headroom
from daimon.core.session_preparation_stages import (
    FreshSessionFactory,
    PreparationStageName,
    PreparedReplacement,
    WorkspaceTransfer,
    desired_snapshot_for,
    failed_stage,
    failure_reason,
    lock_preparation,
    recorded_snapshot,
    retry_after,
    turn_is_active,
)
from daimon.core.session_seal import inherited_seal_ids, session_facts
from daimon.core.session_snapshot import SessionSnapshot, fingerprint_identity, fingerprint_mutable
from daimon.core.session_update_ops import (
    AppliedOps,
    EnvMountLost,
    SessionBusy,
    apply_update_ops,
)
from daimon.core.stores.access_policy import load_access_policy, lock_access_policy
from daimon.core.stores.domain import ThreadSessionRow, TransferKind
from daimon.core.stores.session_preparations import (
    advance_stage,
    fail_preparation,
    upsert_preparation,
)
from daimon.core.stores.thread_agent_bindings import get_binding_by_id
from daimon.core.stores.thread_session_lineage import (
    clear_fresh_start,
    mark_retired,
    mark_superseded,
)
from daimon.core.stores.thread_sessions import get_thread_session_by_id, update_mutable_fingerprint
from daimon.core.thread_handoff import destination_may_read
from daimon.core.turn.admission import (
    Admission,
    decide_before_send,
    reauthorize,
    restrict_inherited_memory,
)
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import (
    AdmissionDenied,
    DmSourceSealedError,
    SessionAgentMismatch,
    SessionBusyError,
)
from daimon.core.turn.posture import UsageRecorder
from daimon.core.turn.prepare import ContinuityOutcome, FreshSession, PreparedTurn
from daimon.core.turn.session_identity import check_session_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

CONTINUED = ContinuityOutcome()

__all__ = [
    "PreparationBusy",
    "PreparationDeferred",
    "PreparationFailure",
    "PreparedReplacement",
    "SessionOps",
    "WorkspaceTransfer",
    "prepare_session_for_turn",
]

# How long a caller is told to wait when a responder change lands on a thread
# whose previous turn has not finished. Short on purpose: the wait is for the
# in-flight turn to end, and the retry is the caller's next message either way.
BUSY_RETRY_S = 5


@dataclass(frozen=True, slots=True)
class PreparationDeferred:
    """Run this turn as it is; the change lands at the caller's next message."""

    prepared: PreparedTurn
    pending_reasons: tuple[ChangeReason, ...]


@dataclass(frozen=True, slots=True)
class PreparationBusy:
    """A responder change arrived while the outgoing responder's turn still runs.

    Deliberately not a `PreparationDeferred`: deferring means "run this turn on
    the CURRENT session", and the current session is the one being taken away —
    it belongs to the agent handing the task over, with that agent's system
    prompt, skills, memory store, vault and `.env`. Running the incoming
    responder's turn there executes one agent's request inside another agent's
    workspace while the footer credits the incoming one. No turn runs; the
    change is made at the caller's next message, once the marker has cleared.
    """

    pending_reasons: tuple[ChangeReason, ...]
    retry_after: dt.datetime


@dataclass(frozen=True, slots=True)
class PreparationFailure:
    """The change did not happen and the turn must not run.

    `preserved` is the claim the copy rests on: the old session was never torn
    down, so the caller's work is exactly where they left it.
    """

    reasons: tuple[ChangeReason, ...]
    stage: PreparationStageName
    retry_after: dt.datetime
    preserved: bool = True


class RecorderFactory(Protocol):
    """`turn.prepare.bind_recorder`, injected for the same reason as the factory."""

    def __call__(
        self,
        deps: TurnDeps,
        *,
        tenant_id: uuid.UUID,
        external_user_id: str,
        ma_session_id: str,
        model_id: str,
        channel_id: str | None = None,
    ) -> UsageRecorder: ...


class LiveRowReader(Protocol):
    """`stores.thread_sessions.get_live_thread_session`, injected likewise."""

    async def __call__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        platform: str,
        thread_id: str,
        account_id: uuid.UUID,
    ) -> ThreadSessionRow | None: ...


@dataclass(frozen=True, slots=True)
class SessionOps:
    """The three primitives `turn.prepare` owns, passed in rather than imported.

    Session creation has to live in `turn.prepare` (it is the module every
    adapter test patches `create_session` on), and that module calls this one —
    so the dependency runs one way and these come back through the door.
    """

    read_live_row: LiveRowReader
    create_fresh: FreshSessionFactory
    bind_record: RecorderFactory


@dataclass(frozen=True, slots=True)
class _Replaced:
    """A successor session and the outcome that describes the switch."""

    fresh: FreshSession
    continuity: ContinuityOutcome


async def _handoff_authorizes(deps: TurnDeps, admission: Admission) -> bool:
    """Did someone hand this thread's task to the agent now answering?

    Without a handoff binding a responder change is `SessionAgentMismatch`: one
    agent's workspace is not another's to take over. With one, the switch was
    asked for, and replacing the session is the point.
    """
    binding_id = admission.config.thread_binding_id
    if binding_id is None:
        return False
    async with deps.sessionmaker() as session:
        binding = await get_binding_by_id(session, id=binding_id)
    return (
        binding is not None
        and binding.kind == "handoff"
        and binding.responder_ma_agent_id == admission.agent.id
    )


async def _destination_may_carry(
    deps: TurnDeps, admission: Admission, row: ThreadSessionRow
) -> bool:
    """Whether the agent taking this thread over may be given the old session's work.

    Decided like a transcript read (`thread_handoff.destination_may_read`), on
    the policy as it is now and the seal the old session recorded. A session
    that can't be read is never carried.
    """
    try:
        previous = await retrieve_session_record(
            deps.anthropic,
            row.ma_session_id,
            scope=session_scope(
                tenant_id=row.tenant_id,
                account_id=row.account_id,
                call_site="session_preparation:retrieve",
            ),
        )
    except anthropic_pkg.APIError as error:
        log.warning(
            "session_preparation.handoff_source_unreadable",
            session_id=row.ma_session_id,
            error=type(error).__name__,
        )
        return False
    async with deps.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=row.tenant_id)
    agent = (
        admission.grant.agent
        if admission.grant is not None
        else build_agent_ref(
            admission.agent.name, admission.agent.metadata, admission.config.agent_name
        )
    )
    allowed = destination_may_read(
        policy,
        agent=agent,
        channel_id=admission.origin_channel_id,
        thread_id=admission.origin_thread_id,
        previous=session_facts(previous.metadata, owned=True),
    )
    if not allowed:
        log.info(
            "session_preparation.handoff_carries_nothing",
            session_id=row.ma_session_id,
            reason="outside_source_seal",
        )
    return allowed


async def _persist_refresh(
    sessionmaker: async_sessionmaker[AsyncSession], *, id: uuid.UUID, snapshot: SessionSnapshot
) -> None:
    async with sessionmaker() as session, session.begin():
        await update_mutable_fingerprint(
            session, id=id, snapshot=snapshot, mutable_fingerprint=fingerprint_mutable(snapshot)
        )


async def _heal_lineage(deps: TurnDeps, session: AsyncSession, *, row: ThreadSessionRow) -> None:
    """Finish a supersede that a crash left half-done.

    The successor is written and committed before the old row is superseded —
    deliberately, so a crash between them leaves a working session rather than
    none. It also leaves the old row marked `live`, invisible (the lookup
    returns the newest) but never closed. The next bind on the successor is
    where that gets tidied, at the cost of one primary-key read for the only
    rows that can be in that state: the ones with a predecessor.
    """
    if row.predecessor_id is None:
        return
    predecessor = await get_thread_session_by_id(session, id=row.predecessor_id)
    if predecessor is None or predecessor.status != "live":
        return
    log.info(
        "session_preparation.supersede_healed",
        mapping_id=str(row.id),
        predecessor_id=str(row.predecessor_id),
    )
    await lock_session_mutation(session, predecessor.ma_session_id, check=False)
    with contextlib.suppress(anthropic_pkg.NotFoundError):
        await archive_session_record(
            deps.anthropic,
            predecessor.ma_session_id,
            scope=session_scope(
                tenant_id=predecessor.tenant_id,
                account_id=predecessor.account_id,
                call_site="session_preparation:heal_lineage",
            ),
        )
    async with deps.sessionmaker.begin() as closing:
        await lock_access_policy(closing, tenant_id=row.tenant_id)
        await mark_superseded(closing, id=row.predecessor_id, replaced_by_id=row.id)


async def _close_out(
    session: AsyncSession, *, row: ThreadSessionRow, new_mapping_id: uuid.UUID, fresh_start: bool
) -> None:
    """Stop the old row being live, now that a working successor exists.

    Deliberately the inverse order of the dead-session path in `turn.run`: that
    one marks dead first because the session is already gone, while here the
    old session holds the only copy of the work until the successor has it.
    """
    if fresh_start:
        await mark_retired(session, id=row.id)
        await clear_fresh_start(session, id=row.id)
    else:
        await mark_superseded(session, id=row.id, replaced_by_id=new_mapping_id)


async def _run_replacement(
    deps: TurnDeps,
    admission: Admission,
    *,
    ops: SessionOps,
    row: ThreadSessionRow,
    recorded: SessionSnapshot | None,
    desired: SessionSnapshot,
    reasons: tuple[ChangeReason, ...],
    fresh_start: bool,
    transfer: WorkspaceTransfer | None,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    session_account_id: uuid.UUID,
    deadline: dt.datetime,
    now: dt.datetime,
) -> _Replaced | PreparationFailure:
    """Create the successor, carry the work into it, then retire the old row.

    The caller's answer about uncommitted repository changes rides on `row`
    and reaches the transfer hook from here; `_close_out` clears it, so one
    answer governs exactly one replacement.
    """
    async with deps.sessionmaker() as session, session.begin():
        preparation = await upsert_preparation(
            session, mapping_id=row.id, target_fingerprint=fingerprint_identity(desired)
        )

    if preparation.stage == "failed":
        due = retry_after(preparation)
        if now < due:
            return PreparationFailure(
                reasons=reasons, stage=failed_stage(preparation.failure_reason), retry_after=due
            )

    replacement: PreparedReplacement | None = None
    stage: PreparationStageName = "create"
    try:
        if transfer is not None and recorded is not None and not fresh_start:
            stage = "checkpoint"
            await _advance(deps, preparation.id, "checkpointed", now)
            replacement = await transfer(
                old_session_id=row.ma_session_id,
                old_snapshot=recorded,
                transfer_id=preparation.id,
                deadline=deadline,
                destination_model_id=admission.agent.model.id,
                destination_agent_name=admission.agent.name,
                requested_work=None,
                unsaved_work=row.pending_unsaved_work,
                before_send=decide_before_send(deps, admission),
            )
            stage = "upload"
            await _advance(
                deps,
                preparation.id,
                "uploaded",
                now,
                transfer_file_id=replacement.transfer_file_id,
                transfer_kind=replacement.transfer_kind,
            )
        stage = "create"
        fresh = await ops.create_fresh(
            deps,
            admission,
            tenant_id=tenant_id,
            platform=platform,
            thread_id=thread_id,
            session_account_id=session_account_id,
            extra_resources=() if replacement is None else replacement.extra_resources,
            predecessor_id=row.id,
            transfer_file_id=None if replacement is None else replacement.transfer_file_id,
            transfer_kind=None if replacement is None else replacement.transfer_kind,
        )
    except (anthropic_pkg.APIError, DaimonError) as error:
        async with deps.sessionmaker() as session, session.begin():
            await fail_preparation(
                session, id=preparation.id, reason=failure_reason(stage, error), now=now
            )
        log.warning(
            "session_preparation.failed",
            stage=stage,
            mapping_id=str(row.id),
            session_id=row.ma_session_id,
            error=str(error),
        )
        if isinstance(error, AdmissionDenied | DmSourceSealedError | SessionBusyError):
            # Decided again just before the successor was created: refused or
            # deferred for a seal, not a failed preparation.
            raise error
        return PreparationFailure(reasons=reasons, stage=stage, retry_after=now)

    async with deps.sessionmaker() as session, session.begin():
        await advance_stage(
            session, id=preparation.id, stage="created", now=now, new_mapping_id=fresh.mapping_id
        )
    with contextlib.suppress(anthropic_pkg.NotFoundError):
        await archive_session_record(
            deps.anthropic,
            row.ma_session_id,
            scope=session_scope(
                tenant_id=row.tenant_id,
                account_id=row.account_id,
                call_site="session_preparation:replacement",
            ),
        )
    async with deps.sessionmaker() as session, session.begin():
        await lock_access_policy(session, tenant_id=row.tenant_id)
        await _close_out(session, row=row, new_mapping_id=fresh.mapping_id, fresh_start=fresh_start)
        await advance_stage(session, id=preparation.id, stage="completed", now=now)

    log.info(
        "session_preparation.replaced",
        reasons=list(reasons),
        old_session_id=row.ma_session_id,
        new_session_id=fresh.ma_session_id,
        transfer_kind=None if replacement is None else replacement.transfer_kind,
    )
    return _Replaced(fresh=fresh, continuity=_replaced_outcome(reasons, replacement))


def _replaced_outcome(
    reasons: tuple[ChangeReason, ...], replacement: PreparedReplacement | None
) -> ContinuityOutcome:
    """Describe the switch, using what the transfer actually managed to carry.

    No transfer means nothing was carried and `transfer_kind` stays None — the
    copy this feeds must not claim work moved when none did.
    """
    if replacement is None:
        return ContinuityOutcome(state="replaced", applied=reasons)
    return ContinuityOutcome(
        state="replaced",
        applied=reasons,
        transfer_kind=replacement.transfer_kind,
        user_prefix=replacement.user_prefix,
        system_blocks=replacement.system_blocks,
    )


async def _advance(
    deps: TurnDeps,
    preparation_id: uuid.UUID,
    stage: Literal["checkpointed", "uploaded"],
    now: dt.datetime,
    *,
    transfer_file_id: str | None = None,
    transfer_kind: TransferKind | None = None,
) -> None:
    async with deps.sessionmaker() as session, session.begin():
        await advance_stage(
            session,
            id=preparation_id,
            stage=stage,
            now=now,
            transfer_file_id=transfer_file_id,
            transfer_kind=transfer_kind,
        )


async def prepare_session_for_turn(
    deps: TurnDeps,
    admission: Admission,
    *,
    ops: SessionOps,
    tenant_id: uuid.UUID,
    platform: str,
    external_user_id: str,
    thread_id: str,
    session_account_id: uuid.UUID,
    reuse_existing: bool,
    capabilities: MaCapabilities = DEFAULT_MA_CAPABILITIES,
    transfer: WorkspaceTransfer | None = None,
    deadline: dt.datetime | None = None,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> PreparedTurn | PreparationDeferred | PreparationBusy | PreparationFailure:
    """Queue before the advisory-lock transaction checks out a connection."""

    async def attempt():
        async with deps.preparation_gate.hold(), pool_headroom(deps.sessionmaker, preparation=True):
            return await _prepare_session_for_turn_locked(
                deps,
                admission,
                ops=ops,
                tenant_id=tenant_id,
                platform=platform,
                external_user_id=external_user_id,
                thread_id=thread_id,
                session_account_id=session_account_id,
                reuse_existing=reuse_existing,
                capabilities=capabilities,
                transfer=transfer,
                deadline=deadline,
                now=now,
            )

    return await retry_fences(attempt)


async def _prepare_session_for_turn_locked(
    deps: TurnDeps,
    admission: Admission,
    *,
    ops: SessionOps,
    tenant_id: uuid.UUID,
    platform: str,
    external_user_id: str,
    thread_id: str,
    session_account_id: uuid.UUID,
    reuse_existing: bool,
    capabilities: MaCapabilities = DEFAULT_MA_CAPABILITIES,
    transfer: WorkspaceTransfer | None = None,
    deadline: dt.datetime | None = None,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> PreparedTurn | PreparationDeferred | PreparationBusy | PreparationFailure:
    """Find, refresh or replace this caller's session, then bind its recorder.

    Billing binds to the model the bound session actually runs — the fresh
    session's own snapshot, or the reused row's recorded one — never the
    responder agent's current model, which a live session never picked up.
    """
    moment = now()
    bound_deadline = deadline if deadline is not None else moment

    def prepared(
        *,
        ma_session_id: str,
        mapping_id: uuid.UUID | None,
        model_id: str,
        watermark: str | None,
        reused: bool,
        continuity: ContinuityOutcome,
        decided: Admission | None = None,
    ) -> PreparedTurn:
        current = decided or admission
        return PreparedTurn(
            admission=current,
            ma_session_id=ma_session_id,
            mapping_id=mapping_id,
            watermark=watermark,
            reused=reused,
            session_account_id=session_account_id,
            _record=ops.bind_record(
                deps,
                tenant_id=tenant_id,
                external_user_id=external_user_id,
                ma_session_id=ma_session_id,
                model_id=model_id,
                channel_id=current.budget_channel_id,
            ),
            continuity=continuity,
        )

    def from_fresh(fresh: FreshSession, continuity: ContinuityOutcome) -> PreparedTurn:
        return prepared(
            ma_session_id=fresh.ma_session_id,
            mapping_id=fresh.mapping_id,
            model_id=fresh.snapshot.model_id,
            watermark=None,
            reused=False,
            continuity=continuity,
            # The admission decided again right before the session was created.
            decided=fresh.admission,
        )

    async with deps.sessionmaker() as db, db.begin():
        await lock_preparation(
            db,
            tenant_id=tenant_id,
            platform=platform,
            thread_id=thread_id,
            account_id=session_account_id,
        )
        # The lock wait can be long: decide again before anything here runs the
        # old session (a checkpoint) or compares memory access. A pin refuses; a
        # seal added meanwhile makes this preparation read-only.
        admission = await reauthorize(deps, admission)
        row = (
            await ops.read_live_row(
                db,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                account_id=session_account_id,
            )
            if reuse_existing
            else None
        )

        if row is None:
            fresh = await ops.create_fresh(
                deps,
                admission,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                session_account_id=session_account_id,
            )
            return from_fresh(fresh, CONTINUED)

        await _heal_lineage(deps, db, row=row)

        handed_over = False
        source_exists = True
        recorded: SessionSnapshot | None = None
        try:
            identity = await check_session_agent(
                deps.anthropic,
                deps.sessionmaker,
                mapping=row,
                responder_ma_agent_id=admission.agent.id,
            )
        except SessionAgentMismatch:
            if not await _handoff_authorizes(deps, admission):
                raise
            handed_over = True
            recorded = row.effective_config
        else:
            source_exists = identity.session_exists
            if identity.session_exists:
                recorded = await recorded_snapshot(
                    deps.anthropic, deps.sessionmaker, existing=row, observed=identity.observed
                )
            if recorded is None and not admission.memory_read_only:
                # No readable configuration to compare: the session is gone or
                # unreadable, which the turn's own recovery handles. Bind it as
                # every pre-continuity turn did.
                return prepared(
                    ma_session_id=row.ma_session_id,
                    mapping_id=row.id,
                    model_id=admission.agent.model.id,
                    watermark=row.watermark_message_id,
                    reused=True,
                    continuity=CONTINUED,
                )

        # New rows carry authoritative seals; legacy rows are read only on a
        # handoff, while ordinary legacy reuse keeps its existing MA call budget.
        inherited = frozenset(row.seal_ids or ())
        if handed_over and row.seal_ids is None:
            inherited = await inherited_seal_ids(
                deps.anthropic,
                predecessor_session_id=row.ma_session_id,
                own_thread_id=thread_id,
                tenant_seals_anything=bool(admission.origin_seal_ids),
                scope=session_scope(
                    tenant_id=row.tenant_id,
                    account_id=row.account_id,
                    call_site="session_preparation:inherited_seal_ids",
                ),
            )
        admission = await restrict_inherited_memory(deps, admission, inherited)
        agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(admission.agent.id))
        desired = await desired_snapshot_for(
            deps.sessionmaker,
            agent=admission.agent,
            environment_id=admission.environment.id,
            tenant_id=tenant_id,
            agent_uuid=agent_uuid,
            account_id=admission.account_id,
            github_app=deps.agent_github_app,
            fernet=deps.fernet,
            is_external=admission.is_external,
            recorded=recorded,
            tool_safety=deps.tool_safety,
            public_url=deps.public_url,
            memory_read_only=admission.memory_read_only,
            asks_before_publishing=admission.asks_before_publishing,
            channel_skills=admission.channel_skills,
        )
        fresh_start = row.fresh_start_requested_at is not None
        if handed_over:
            decision = ReplaceSession(reasons=("agent_identity",))
        elif recorded is None:
            # A restricted turn must get a new read-only session when the
            # previous mount cannot be established, never reuse unknown access.
            decision = ReplaceSession(reasons=("memory_access",))
        elif fresh_start:
            decision = ReplaceSession(reasons=())
        else:
            assert recorded is not None, "a readable row always has a recorded snapshot here"
            decision = decide_session_compatibility(
                recorded=recorded, desired=desired, capabilities=capabilities, now=moment
            )

        def on_current(continuity: ContinuityOutcome, model_id: str) -> PreparedTurn:
            return prepared(
                ma_session_id=row.ma_session_id,
                mapping_id=row.id,
                model_id=model_id,
                watermark=row.watermark_message_id,
                reused=True,
                continuity=continuity,
            )

        current_model = admission.agent.model.id if recorded is None else recorded.model_id

        tightening_memory = admission.memory_read_only and (
            recorded is None or not recorded.memory_read_only
        )
        if (
            isinstance(decision, ReplaceSession)
            and ("agent_identity" in decision.reasons or tightening_memory)
            and turn_is_active(row, now=moment)
        ):
            # Neither a different responder nor stricter memory access may
            # execute against the old session while replacement is deferred.
            log.info(
                "session_preparation.busy",
                mapping_id=str(row.id),
                session_id=row.ma_session_id,
                reasons=list(decision.reasons),
            )
            return PreparationBusy(
                pending_reasons=decision.reasons,
                retry_after=moment + dt.timedelta(seconds=BUSY_RETRY_S),
            )

        if not isinstance(decision, ReuseAsIs) and turn_is_active(row, now=moment):
            # Uniformly deferred: MA itself refuses `sessions.update` mid-turn,
            # and swapping a running session's `.env` or checkpointing it is a
            # race we have no reason to run.
            return PreparationDeferred(
                prepared=on_current(ContinuityOutcome(pending=decision.reasons), current_model),
                pending_reasons=decision.reasons,
            )

        if isinstance(decision, ReplaceSession):
            # A handoff carries the old work only to an agent that could read
            # it itself from here; otherwise the new agent starts with nothing.
            carry = not handed_over or await _destination_may_carry(deps, admission, row)
            await lock_session_mutation(db, row.ma_session_id)
            observed = None
            if source_exists:
                try:
                    observed = await retrieve_session_record(
                        deps.anthropic,
                        row.ma_session_id,
                        scope=session_scope(
                            tenant_id=row.tenant_id,
                            account_id=row.account_id,
                            call_site="session_preparation:retrieve",
                        ),
                    )
                except anthropic_pkg.NotFoundError:
                    pass
                except anthropic_pkg.APIError as error:
                    log.warning(
                        "session_preparation.replacement_source_unreadable",
                        session_id=row.ma_session_id,
                        error=type(error).__name__,
                    )
                    return PreparationBusy(
                        pending_reasons=decision.reasons,
                        retry_after=moment + dt.timedelta(seconds=BUSY_RETRY_S),
                    )
            if observed is not None and observed.status not in ("idle", "terminated"):
                raise SessionBusyError(
                    pending_reasons=("session_active",),
                    retry_after=moment + dt.timedelta(seconds=BUSY_RETRY_S),
                )
            outcome = await _run_replacement(
                deps,
                admission,
                ops=ops,
                row=row,
                recorded=recorded,
                desired=desired,
                reasons=decision.reasons,
                fresh_start=fresh_start,
                # A checkpoint executes the old session. Never run it with
                # a writable mount after the origin has become read-only.
                transfer=None if tightening_memory or not carry else transfer,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                session_account_id=session_account_id,
                deadline=bound_deadline,
                now=moment,
            )
            if isinstance(outcome, PreparationFailure):
                return outcome
            return from_fresh(outcome.fresh, outcome.continuity)

        assert recorded is not None, "an in-place path always has a recorded snapshot"
        applying: tuple[UpdateOp, ...] = decision.ops if isinstance(decision, UpdateInPlace) else ()
        if not any(isinstance(op, RemirrorVaultCredentials) for op in applying):
            # Every reused session has re-mirrored the caller's vault on every
            # turn since that path existed; a compatible session keeps doing so.
            applying = (*applying, RemirrorVaultCredentials())

        async def run_ops() -> AppliedOps | SessionBusy:
            return await apply_update_ops(
                deps.anthropic,
                deps.sessionmaker,
                ops=applying,
                session_id=row.ma_session_id,
                recorded=recorded,
                agent=admission.agent,
                tenant_id=tenant_id,
                agent_uuid=agent_uuid,
                account_id=admission.account_id,
                mcp=deps.mcp,
                tool_safety=deps.tool_safety,
                fernet=deps.fernet,
                github_fallback_pat=deps.github_fallback_pat,
                github_app_id=deps.github_app_id,
                github_app_private_key=deps.github_app_private_key,
                agent_github_app=deps.agent_github_app,
                is_external=admission.is_external,
                now=moment,
                asks_before_publishing=admission.asks_before_publishing,
            )

        if isinstance(decision, ReuseAsIs):
            # Nothing to apply but the vault mirror, whose failures have always
            # propagated to the adapter's error edge rather than deferring.
            # The mirror can await Anthropic and MCP. The advisory lock has
            # finished its decision; do not keep a pooled connection checked
            # out for that network round trip on every compatible turn.
            await db.commit()
            await run_ops()
            async with deps.sessionmaker() as check:
                current_row = await ops.read_live_row(
                    check,
                    tenant_id=tenant_id,
                    platform=platform,
                    thread_id=thread_id,
                    account_id=session_account_id,
                )
            if (
                current_row is None
                or current_row.id != row.id
                or current_row.identity_fingerprint
                != (
                    fingerprint_identity(recorded)
                    if row.effective_config is None
                    else row.identity_fingerprint
                )
                or current_row.mutable_fingerprint
                != (
                    fingerprint_mutable(recorded)
                    if row.effective_config is None
                    else row.mutable_fingerprint
                )
                or current_row.fresh_start_requested_at != row.fresh_start_requested_at
            ):
                raise SessionBusyError(
                    pending_reasons=("session_changed",),
                    retry_after=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1),
                )
            return on_current(CONTINUED, recorded.model_id)

        try:
            result = await run_ops()
        except EnvMountLost as error:
            # The old `.env` is gone: record that, so the next bind's decision
            # is an add rather than another delete-then-add.
            await _persist_refresh(deps.sessionmaker, id=row.id, snapshot=error.snapshot)
            log.warning("session_preparation.env_refresh_failed", session_id=row.ma_session_id)
            return PreparationFailure(reasons=decision.reasons, stage="update", retry_after=moment)
        except (anthropic_pkg.APIError, DaimonError) as error:
            log.warning(
                "session_preparation.update_failed",
                session_id=row.ma_session_id,
                error=str(error),
            )
            return PreparationFailure(reasons=decision.reasons, stage="update", retry_after=moment)

        applied: tuple[ChangeReason, ...] = tuple(
            reason for reason in decision.reasons if reason in result.applied
        )
        pending: tuple[ChangeReason, ...] = tuple(
            reason for reason in decision.reasons if reason not in result.applied
        )
        if applied:
            await _persist_refresh(deps.sessionmaker, id=row.id, snapshot=result.snapshot)
        outcome_state: Literal["continued", "updated"] = "updated" if applied else "continued"
        continuity = ContinuityOutcome(state=outcome_state, applied=applied, pending=pending)
        if isinstance(result, SessionBusy):
            return PreparationDeferred(
                prepared=on_current(continuity, result.snapshot.model_id),
                pending_reasons=pending,
            )
        log.info(
            "session_preparation.refreshed",
            session_id=row.ma_session_id,
            applied=list(applied),
        )
        return on_current(continuity, result.snapshot.model_id)
