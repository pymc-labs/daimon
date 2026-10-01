"""Carry one task's workspace from a retired MA session into its successor.

A session is a creation-time snapshot of its agent: model, prompt, skills,
environment and repo are frozen at create. Changing any of those, or handing
the task to a different responder, therefore means a NEW session — and a new
session starts with an empty disk and no conversation. This module is the one
path by which the old workspace reaches the new one.

The ladder, best first, each rung the fallback for the one above:

1. **Full handoff** — spend one billed, bounded turn on the OLD session
   asking it to write a handoff note and tar its own working files into
   `/mnt/session/outputs` (`daimon.core.checkpoint_prompt`), then download
   that archive, re-upload it as a standalone file, and hand the caller a
   file resource to mount in the successor's `sessions.create`. The Files API
   lists only session outputs and mounted uploads, so a session that does not
   pack its own files has none that daimon can see (capability matrix P4.b).
2. **Transcript only** — the old session is dead, the checkpoint failed or
   timed out, the bundle was oversized, or there was no work worth spending a
   turn on. The conversation still crosses, quoted; the files do not. An
   oversized bundle is deleted on the way down this rung — by the checkpoint
   turn itself where its own size guard caught it, by this module where the
   listing did — so a rejected transfer leaves nothing behind.
3. **History only** — even `events.list` is gone (the session was deleted).
   Nothing crosses here; the platform thread is all the successor has.

Which rung was reached is recorded in `transfer_kind` and phrased for the
user by `daimon.core.handoff_context`, so the copy can never claim more than
actually happened.

Shell, not core: every step here is I/O. The decisions it consumes
(`is_worth_checkpointing`, `select_recent_turns`, `render_handoff_framing`)
and the words it sends (`build_checkpoint_prompt`) are pure and live in
`handoff_context` and `checkpoint_prompt`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import io
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import anthropic
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsSystemContentBlockParam,
    FileMetadata,
)
from anthropic.types.beta.beta_managed_agents_file_resource_params import (
    BetaManagedAgentsFileResourceParams,
)
from daimon.core.checkpoint_prompt import (
    CHECKPOINT_BUNDLE_MOUNT_PATH,
    CHECKPOINT_OUTPUTS_DIR,
    HANDOFF_MAX_BYTES,
    build_checkpoint_prompt,
    checkpoint_head_lines,
    checkpoint_too_large_bytes,
    handoff_filename,
    is_handoff_filename,
)
from daimon.core.handoff_context import (
    is_worth_checkpointing,
    render_checkpoint_controls,
    render_handoff_framing,
    render_previous_session,
    select_recent_turns,
    supports_system_message,
)
from daimon.core.ma import replay_events
from daimon.core.pricing import MODEL_PRICING
from daimon.core.session_preparation_stages import PreparedReplacement
from daimon.core.session_snapshot import SessionSnapshot
from daimon.core.stores.domain import UnsavedWorkChoice
from daimon.core.stores.pending_file_deletes import enqueue_pending_file_delete
from daimon.core.turn.driver import run_turn
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.posture import AutoApprove, Billed, UsageRecorder
from daimon.core.turn.state import TurnState, extract_final_response
from daimon.core.usage_recording import record_turn_usage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

_MA_BETA = "managed-agents-2026-04-01"
_MIB = 1024 * 1024

#: Where the successor mounts the bundle. Absolute and at the container root
#: so the extraction command in the framing text is one line; MA joins any
#: mount path under `/mnt/session/uploads/` regardless (P4.d).
HANDOFF_MOUNT_PATH = "/daimon-handoff.tar.gz"

#: Why the workspace is being replaced, as the checkpoint controls report it.
#: Not a reason code: the model on the giving side reads it.
CHECKPOINT_REASON_HANDOFF = "handoff"
CHECKPOINT_REASON_MODEL_CHANGE = "model change"
CHECKPOINT_REASON_CONFIGURATION_CHANGE = "configuration change"

#: How long the re-uploaded bundle survives in the Files API. Long enough to
#: outlive a failed replacement and a retry, short enough not to accumulate.
#: `files.delete` leaves an already-mounted sandbox copy on disk (P4.f), so
#: expiry never pulls the archive out from under a running successor.
BUNDLE_RETENTION = timedelta(days=7)

# MA's rejection when `events.send` targets a session it has closed. Same
# marker `daimon.core.turn.run` matches for dead-session recovery; a
# checkpoint turn gets no recovery, it degrades to the transcript rung.
_ARCHIVED_SESSION_MARKER = "cannot send events to archived session"

# Poll schedule for the bundle's appearance in the outputs listing. Mirrors
# `output_delivery`'s settle discipline (indexing lag is measured from the
# file WRITE, so a stable-looking first poll proves nothing) and adds a
# size-stability check: a tar still being written lists at a growing size,
# and downloading it half-built would ship a corrupt archive.
_POLL_DELAYS_S = (0.0, 2.0, 4.0, 8.0)
_MIN_SETTLE_S = 6.0

GapReason = Literal[
    "session_dead",
    "checkpoint_failed",
    "checkpoint_timeout",
    "bundle_oversize",
    "upload_failed",
    "not_worth_checkpointing",
    "archive_missing",
]

TransferKind = Literal["full", "transcript", "history"]


@dataclass(frozen=True)
class FullHandoff:
    """The working files crossed, as a mountable archive."""

    transfer_file_id: str
    mount_path: str
    bytes_transferred: int
    transcript: str | None
    unpreserved: tuple[str, ...]


@dataclass(frozen=True)
class TranscriptOnly:
    """The conversation crossed; the files did not, and `gap_reason` says why."""

    transcript: str
    gap_reason: GapReason


@dataclass(frozen=True)
class HistoryOnly:
    """Neither files nor conversation could be read from the old session."""

    gap_reason: Literal["events_unavailable"]


TransferOutcome = FullHandoff | TranscriptOnly | HistoryOnly


# What each gap rung means to a person, as a noun phrase — `handoff_context`
# renders these into "Not carried over: …". Never a reason code.
_GAP_PHRASING: dict[GapReason | Literal["events_unavailable"], str] = {
    "session_dead": "the previous workspace's files, because its workspace had already closed",
    "checkpoint_failed": "the previous workspace's files, because saving them did not finish",
    "checkpoint_timeout": "the previous workspace's files, because saving them ran out of time",
    "bundle_oversize": "the previous workspace's files, because together they were too large "
    "to carry",
    "upload_failed": "the previous workspace's files, because the saved archive could not be "
    "transferred",
    "not_worth_checkpointing": "the previous workspace's files, of which there were none yet",
    "archive_missing": "the previous workspace's files, because the saved archive was gone "
    "before it could be read",
    "events_unavailable": "the previous workspace's files and the previous conversation",
}

_COMMITTED_DURING_CHECKPOINT = "the agent committed to the repository during checkpoint"

# What `leave` costs, in the same noun-phrase shape as `_GAP_PHRASING`: the
# person was asked and chose this, so the successor is told plainly rather
# than left to discover the changes missing.
_UNSAVED_WORK_LEFT_BEHIND = "uncommitted repository changes were left in the old checkout"


class _NoOpLifecycle(TurnLifecycle):
    """A checkpoint turn has no surface to render to.

    Its reply is read from the returned `TurnState`, not delivered anywhere:
    nobody is watching this turn and its output is an archive, not an answer.
    Inherits the protocol's own no-op bodies for the optional hooks.
    """

    async def on_render(self, state: TurnState) -> None:
        return None

    async def on_terminal_success(self, state: TurnState) -> None:
        return None

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_not_found(err: anthropic.APIStatusError) -> bool:
    return err.status_code == 404


def _checkpoint_gap_reason(state: TurnState) -> GapReason | None:
    """Why the checkpoint turn failed, or None when it succeeded.

    The driver folds failures into `TurnState.error` rather than raising, so
    this reads the error rather than catching one. A 404 or the archived-400
    both mean the old session can never accept another event; the wall-clock
    ceiling is its own rung because a timed-out checkpoint may still be
    writing, and everything else is an ordinary failure.
    """
    err = state.error
    if err is None:
        return None
    if err.kind == "ceiling":
        return "checkpoint_timeout"
    cause = err.cause
    if isinstance(cause, anthropic.APIStatusError) and (
        _is_not_found(cause)
        or (cause.status_code == 400 and _ARCHIVED_SESSION_MARKER in str(cause).lower())
    ):
        return "session_dead"
    return "checkpoint_failed"


def _bind_checkpoint_recorder(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    external_user_id: str,
    ma_session_id: str,
    model_id: str,
    markup: Decimal,
) -> UsageRecorder:
    """Meter the checkpoint turn to the tenant, at the OLD session's model.

    Shaped like `turn.prepare.bind_recorder` and for the same reason — a
    session runs the model it froze at create, so that is the model its work
    is priced at — but bound here rather than there because a transfer has no
    `TurnDeps` to hand, and because this turn's ledger rows carry
    `checkpoint_debit` so the spend is separable from turns a person asked
    for.
    """
    pricing = MODEL_PRICING.get(model_id)
    if pricing is None:
        # The turn still runs and still records usage; only the debit is zero.
        log.warning("billing.unpriced_model", model_id=model_id, ma_session_id=ma_session_id)
    return functools.partial(
        record_turn_usage,
        sessionmaker=sessionmaker,
        platform_user_id=external_user_id,
        managed_session_id=ma_session_id,
        model_id=model_id,
        tenant_id=tenant_id,
        markup=markup,
        pricing=pricing,
        reason="checkpoint_debit",
    )


async def _poll_for_bundle(
    client: AsyncAnthropic,
    *,
    session_id: str,
    sleep: Callable[[float], Awaitable[None]],
) -> FileMetadata | None:
    """The session's handoff bundle once its size stops changing, or None.

    Settles only once cumulative poll time has reached `_MIN_SETTLE_S` AND
    two consecutive polls agree on `size_bytes`. An exhausted schedule
    returns the last observation, stable or not — the caller would rather
    try a possibly-truncated archive than silently drop the handoff.
    """
    latest: FileMetadata | None = None
    previous_size: int | None = None
    elapsed = 0.0
    for delay in _POLL_DELAYS_S:
        if delay > 0:
            await sleep(delay)
        elapsed += delay
        page = await client.beta.files.list(scope_id=session_id, betas=[_MA_BETA], limit=1000)
        latest = next(
            (
                meta
                for meta in page.data
                if meta.downloadable is True and is_handoff_filename(meta.filename)
            ),
            None,
        )
        if latest is None:
            previous_size = None
            continue
        if elapsed >= _MIN_SETTLE_S and latest.size_bytes == previous_size:
            break
        previous_size = latest.size_bytes
    return latest


async def _rehost_bundle(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    bundle: FileMetadata,
    now: Callable[[], datetime],
) -> tuple[str, int] | GapReason:
    """Download the session output, drop it from the listing, re-upload it.

    Re-hosting is what makes the archive mountable: a session-scoped output
    belongs to the session that wrote it and dies with it, while an uploaded
    file is standalone and can be mounted anywhere. Deleting the output first
    keeps the retired session's listing clean and costs nothing — the bytes
    are already in hand, and the sandbox copy survives a delete (P4.f).

    Returns `(file_id, bytes)` or the gap reason to degrade with.
    """
    try:
        response = await client.beta.files.download(bundle.id, betas=[_MA_BETA])
        content = await response.read()
    except anthropic.APIStatusError as err:
        log.warning("workspace_transfer.download_failed", file_id=bundle.id, error=str(err)[:300])
        return "archive_missing"

    # Already gone (a concurrent sweep, a retried transfer) is not an error.
    with contextlib.suppress(anthropic.NotFoundError):
        await client.beta.files.delete(bundle.id, betas=[_MA_BETA])

    try:
        uploaded = await client.beta.files.upload(
            file=(bundle.filename, io.BytesIO(content), "application/gzip"),
        )
    except anthropic.APIStatusError as err:
        log.warning("workspace_transfer.upload_failed", error=str(err)[:300])
        return "upload_failed"

    async with sessionmaker() as session, session.begin():
        await enqueue_pending_file_delete(
            session, file_id=uploaded.id, delete_after=now() + BUNDLE_RETENTION
        )
    return (uploaded.id, len(content))


async def transfer_workspace(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    old_session_id: str,
    old_snapshot: SessionSnapshot,
    tenant_id: uuid.UUID,
    external_user_id: str,
    transfer_id: uuid.UUID,
    markup: Decimal,
    checkpoint_deadline: datetime,
    from_agent_name: str,
    reason: str = CHECKPOINT_REASON_CONFIGURATION_CHANGE,
    unsaved_work: UnsavedWorkChoice | None = None,
    max_bundle_bytes: int = HANDOFF_MAX_BYTES,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] = _utc_now,
) -> TransferOutcome:
    """Walk the ladder for one replacement and report how far it got.

    Spends at most ONE billed turn, on the old session, and only when that
    session actually produced work (`is_worth_checkpointing`). Never
    archives or deletes the old session: retiring the mapping row is the
    caller's job, and the old session stays readable.

    `reason` is why the workspace is being replaced, in words, and reaches the
    old session in the checkpoint turn's controls.

    `unsaved_work` is the person's answer about uncommitted changes in the
    mounted repository: `"leave"` keeps them in the old checkout and out of the
    bundle, anything else (including an unanswered None) captures them. The
    choice reaches the checkpoint prompt and, when it was `"leave"`, is also
    reported as something the successor did not get.

    Never raises for an expected degradation — every rung below the top is a
    returned value. It does raise for an unexpected upstream failure (a 500
    on `events.list`, a DB error), because those are bugs or outages the
    caller's boundary should see, not states to paper over.
    """
    try:
        events = await replay_events(client, session_id=old_session_id)
    except anthropic.APIStatusError as err:
        if not _is_not_found(err):
            raise
        log.info("workspace_transfer.events_unavailable", session_id=old_session_id)
        return HistoryOnly(gap_reason="events_unavailable")

    turns = select_recent_turns(events)
    transcript = render_previous_session(turns, from_agent_name=from_agent_name) if turns else None

    if not is_worth_checkpointing(events):
        log.info("workspace_transfer.skipped_checkpoint", session_id=old_session_id)
        return TranscriptOnly(transcript=transcript or "", gap_reason="not_worth_checkpointing")

    prompt = build_checkpoint_prompt(
        transfer_id=transfer_id,
        repo_mount_path=old_snapshot.repo_mount_path,
        max_bundle_mib=max_bundle_bytes // _MIB,
        unsaved_work=unsaved_work,
    )
    # The instruction is the host's, not a chat participant's, and a model
    # that cannot tell the two apart is right to refuse it: observed live,
    # sonnet-5 read the checkpoint as an injected request to tar a home
    # directory into a delivery folder and declined, costing a billed turn and
    # the work. Delivering the prompt on `system.message` alone did not fix
    # that — the refusals then named a missing `<turn_controls>` as the tell —
    # so the user message now leads with the checkpoint controls and carries
    # the prompt behind them, and the system block repeats it where the model
    # takes one.
    controls = render_checkpoint_controls(
        transfer_id=transfer_id,
        reason=reason,
        archive_path=f"{CHECKPOINT_OUTPUTS_DIR}/{handoff_filename(transfer_id)}",
        carried_to=CHECKPOINT_BUNDLE_MOUNT_PATH,
    )
    privileged = supports_system_message(old_snapshot.model_id)
    checkpoint_system_blocks: tuple[BetaManagedAgentsSystemContentBlockParam, ...] = (
        ({"type": "text", "text": prompt},) if privileged else ()
    )
    state = await run_turn(
        anthropic=client,
        session_id=old_session_id,
        user_message=f"{controls}\n\n{prompt}",
        system_blocks=checkpoint_system_blocks,
        lifecycle=_NoOpLifecycle(),
        cancel=asyncio.Event(),
        now=now,
        billing=Billed(
            record=_bind_checkpoint_recorder(
                sessionmaker,
                tenant_id=tenant_id,
                external_user_id=external_user_id,
                ma_session_id=old_session_id,
                model_id=old_snapshot.model_id,
                markup=markup,
            )
        ),
        # Nobody is watching this turn, so a tool call waiting for approval
        # would simply hang until the deadline.
        tool_confirmation=AutoApprove(),
        deadline=checkpoint_deadline,
    )
    failure = _checkpoint_gap_reason(state)
    if failure is not None:
        log.warning(
            "workspace_transfer.checkpoint_failed",
            session_id=old_session_id,
            gap_reason=failure,
        )
        return TranscriptOnly(transcript=transcript or "", gap_reason=failure)

    reply = extract_final_response(state.content)
    # The prompt's own size guard fired: the session deleted its archive and
    # said how big it was, so there is nothing to poll for and nothing left
    # behind in the old session's outputs.
    reported_bytes = checkpoint_too_large_bytes(reply)
    if reported_bytes is not None:
        log.warning(
            "workspace_transfer.bundle_oversize",
            session_id=old_session_id,
            size_bytes=reported_bytes,
            max_bytes=max_bundle_bytes,
            deleted_by="checkpoint_turn",
        )
        return TranscriptOnly(transcript=transcript or "", gap_reason="bundle_oversize")

    bundle = await _poll_for_bundle(client, session_id=old_session_id, sleep=sleep)
    if bundle is None:
        log.warning("workspace_transfer.no_bundle", session_id=old_session_id)
        return TranscriptOnly(transcript=transcript or "", gap_reason="checkpoint_failed")
    if bundle.size_bytes > max_bundle_bytes:
        log.warning(
            "workspace_transfer.bundle_oversize",
            session_id=old_session_id,
            size_bytes=bundle.size_bytes,
            max_bytes=max_bundle_bytes,
            deleted_by="transfer",
        )
        # Nothing will ever read this archive: the sweep leaves anything named
        # `daimon-handoff-` alone and the pending-delete queue is only fed on a
        # successful upload, so without this the rejected bundle sits in the
        # old session's outputs until the session itself is reclaimed.
        with contextlib.suppress(anthropic.NotFoundError):
            await client.beta.files.delete(bundle.id, betas=[_MA_BETA])
        return TranscriptOnly(transcript=transcript or "", gap_reason="bundle_oversize")

    # The prompt asks for `rev-parse HEAD` before and after the archive step.
    # Two different hashes mean the session committed despite being told not
    # to; that is recorded and told to the successor, not prevented.
    first_head, last_head = checkpoint_head_lines(reply)
    # Only meaningful with a repository mounted: without one there is no
    # checkout for anything to be left in, and the question is never asked.
    unpreserved: tuple[str, ...] = (
        (_UNSAVED_WORK_LEFT_BEHIND,)
        if unsaved_work == "leave" and old_snapshot.repo_mount_path is not None
        else ()
    )
    if first_head is not None and last_head is not None and first_head != last_head:
        log.warning(
            "workspace_transfer.repository_changed",
            session_id=old_session_id,
            head_before=first_head,
            head_after=last_head,
        )
        unpreserved = (*unpreserved, _COMMITTED_DURING_CHECKPOINT)

    rehosted = await _rehost_bundle(client, sessionmaker, bundle=bundle, now=now)
    if isinstance(rehosted, str):
        return TranscriptOnly(transcript=transcript or "", gap_reason=rehosted)

    file_id, size_bytes = rehosted
    log.info(
        "workspace_transfer.completed",
        session_id=old_session_id,
        transfer_file_id=file_id,
        bytes_transferred=size_bytes,
    )
    return FullHandoff(
        transfer_file_id=file_id,
        mount_path=HANDOFF_MOUNT_PATH,
        bytes_transferred=size_bytes,
        transcript=transcript,
        unpreserved=unpreserved,
    )


def as_prepared_replacement(
    outcome: TransferOutcome,
    *,
    destination_model_id: str,
    from_agent_name: str,
    to_agent_name: str,
    requested_work: str | None,
) -> PreparedReplacement:
    """Turn one outcome into the successor's create-time and first-turn inputs.

    Pure. `destination_model_id` is the model the SUCCESSOR will freeze, and
    it decides which channel the framing can use — not the old session's.
    """
    extra_resources: tuple[BetaManagedAgentsFileResourceParams, ...] = ()
    transfer_file_id: str | None = None
    bundle_mount_path: str | None = None
    transfer_kind: TransferKind
    previous_session: str | None
    not_carried: tuple[str, ...]

    if isinstance(outcome, FullHandoff):
        transfer_kind = "full"
        transfer_file_id = outcome.transfer_file_id
        # The successor must be told where MA actually mounts the bundle. The
        # resource is requested at `HANDOFF_MOUNT_PATH`, but every mount path is
        # normalised under /mnt/session/uploads/ (live-verified), which is the
        # path the checkpoint prompt already names to the giving side.
        bundle_mount_path = CHECKPOINT_BUNDLE_MOUNT_PATH
        extra_resources = (
            {
                "type": "file",
                "file_id": outcome.transfer_file_id,
                "mount_path": outcome.mount_path,
            },
        )
        previous_session = outcome.transcript or None
        not_carried = outcome.unpreserved
    elif isinstance(outcome, TranscriptOnly):
        transfer_kind = "transcript"
        previous_session = outcome.transcript or None
        not_carried = (_GAP_PHRASING[outcome.gap_reason],)
    else:
        transfer_kind = "history"
        previous_session = None
        not_carried = (_GAP_PHRASING[outcome.gap_reason],)

    framing = render_handoff_framing(
        model_id=destination_model_id,
        transfer_kind=transfer_kind,
        bundle_mount_path=bundle_mount_path,
        from_agent_name=from_agent_name,
        to_agent_name=to_agent_name,
        requested_work=requested_work,
        previous_session=previous_session,
        not_carried=not_carried,
    )
    return PreparedReplacement(
        extra_resources=extra_resources,
        transfer_file_id=transfer_file_id,
        transfer_kind=transfer_kind,
        user_prefix=framing.user_prefix,
        system_blocks=framing.system.blocks if framing.system is not None else (),
    )


def _checkpoint_reason(
    *,
    old_snapshot: SessionSnapshot,
    from_agent_name: str,
    destination_model_id: str,
    destination_agent_name: str,
) -> str:
    """Why this workspace is being replaced, derived from what changed.

    The `WorkspaceTransfer` protocol carries no reason, so the runner reads
    one off the destination it was handed. A different responder is checked
    first: when the agent changes it is a handoff whatever else moved with it,
    while a different model under the same responder can only be a model
    change. Anything else — instructions, skills, repo, environment — reduces
    to one honest phrase rather than a guess at which of them it was.
    """

    if destination_agent_name != from_agent_name:
        return CHECKPOINT_REASON_HANDOFF
    if destination_model_id != old_snapshot.model_id:
        return CHECKPOINT_REASON_MODEL_CHANGE
    return CHECKPOINT_REASON_CONFIGURATION_CHANGE


@dataclass(frozen=True)
class WorkspaceTransferRunner:
    """`transfer_workspace` + `as_prepared_replacement`, bound to one caller.

    The shape the preparation shell depends on: everything that is fixed for
    a given (tenant, caller, source agent) is bound once here, and each
    replacement supplies only what varies. Constructed at the preparation
    site and called at most once per replacement.
    """

    anthropic: AsyncAnthropic
    sessionmaker: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    external_user_id: str
    markup: Decimal
    from_agent_name: str | None = None
    """Override for the source agent's display name; defaults to the snapshot's."""

    async def __call__(
        self,
        *,
        old_session_id: str,
        old_snapshot: SessionSnapshot,
        transfer_id: uuid.UUID,
        deadline: datetime,
        destination_model_id: str,
        destination_agent_name: str,
        requested_work: str | None,
        unsaved_work: UnsavedWorkChoice | None = None,
    ) -> PreparedReplacement:
        from_agent_name = self.from_agent_name or old_snapshot.agent_name
        outcome = await transfer_workspace(
            self.anthropic,
            self.sessionmaker,
            old_session_id=old_session_id,
            old_snapshot=old_snapshot,
            tenant_id=self.tenant_id,
            external_user_id=self.external_user_id,
            transfer_id=transfer_id,
            markup=self.markup,
            checkpoint_deadline=deadline,
            from_agent_name=from_agent_name,
            reason=_checkpoint_reason(
                old_snapshot=old_snapshot,
                from_agent_name=from_agent_name,
                destination_model_id=destination_model_id,
                destination_agent_name=destination_agent_name,
            ),
            unsaved_work=unsaved_work,
        )
        return as_prepared_replacement(
            outcome,
            destination_model_id=destination_model_id,
            from_agent_name=from_agent_name,
            to_agent_name=destination_agent_name,
            requested_work=requested_work,
        )
