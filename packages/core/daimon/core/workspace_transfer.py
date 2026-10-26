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
   An archive over the cap is rebuilt once by the checkpoint turn without its
   largest files; that partial archive is still a full handoff, and the files
   it dropped are named to the successor as not carried over.
2. **Transcript only** — the old session is dead, the checkpoint failed or
   timed out, the bundle was oversized even after the partial rebuild, or
   there was no work worth spending a turn on. The conversation still
   crosses, quoted; the files do not. An oversized bundle is deleted on the
   way down this rung — by the checkpoint turn itself where its own size
   guard caught it, by this module where the listing did — so a rejected
   transfer leaves nothing behind.
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
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from secrets import token_hex
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
from cryptography.fernet import MultiFernet
from daimon.core.checkpoint_prompt import (
    CHECKPOINT_BUNDLE_MOUNT_PATH,
    CHECKPOINT_OUTPUTS_DIR,
    HANDOFF_MAX_BYTES,
    build_checkpoint_prompt,
    checkpoint_head_lines,
    checkpoint_left_out,
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
from daimon.core.mux_backend import managed_agents
from daimon.core.mux_compat import legacy_call
from daimon.core.output_ports_compat import (
    delete_output_record,
    list_output_records,
    read_output_record,
)
from daimon.core.pricing import MODEL_PRICING
from daimon.core.session_ports_compat import retrieve_session_record, session_scope
from daimon.core.session_preparation_stages import PreparedReplacement
from daimon.core.session_snapshot import SessionSnapshot
from daimon.core.stores.domain import UnsavedWorkChoice
from daimon.core.stores.pending_file_deletes import enqueue_pending_file_delete
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.turn.driver import run_turn, unattended_sends
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.outcomes import current_outcome
from daimon.core.turn.persistence import current_persistence
from daimon.core.turn.posture import Billed, UsageRecorder
from daimon.core.turn.state import TurnState, extract_final_response
from daimon.core.turn.unattended import (
    session_gate,
    session_rules_channel,
    unattended_posture,
)
from daimon.core.turn.unattended_capabilities import (
    ENTRY_POINT_POLICY,
    require_allowed_capabilities,
)
from daimon.core.usage_recording import record_turn_usage
from daimon.core.workspace_ports_compat import restore_transfer_records
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import Scope
from mux.drivers.anthropic import Outputs
from pydantic import JsonValue
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
    "invoker_not_allowed",
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
    "session_dead": "the previous working files, because they had already closed",
    "checkpoint_failed": "the previous working files, because saving them did not finish",
    "checkpoint_timeout": "the previous working files, because saving them ran out of time",
    "bundle_oversize": "the previous working files, because together they were too large to carry",
    "upload_failed": "the previous working files, because the saved archive could not be "
    "transferred",
    "not_worth_checkpointing": "the previous working files, of which there were none yet",
    "archive_missing": "the previous working files, because the saved archive was gone "
    "before it could be read",
    "events_unavailable": "the previous working files and the previous conversation",
    "invoker_not_allowed": "the previous working files, because the previous agent "
    "requires a human role-holder",
}

_COMMITTED_DURING_CHECKPOINT = "the agent committed to the repository during checkpoint"


def _left_out_phrase(paths: tuple[str, ...], more: int) -> str | None:
    """The files a partial archive dropped, as one `_GAP_PHRASING`-style noun phrase."""
    # Backticks keep a comma inside a name from splitting the not-carried list;
    # a name that holds one is counted rather than quoted half-open.
    named = [f"`{path}`" for path in paths if "`" not in path]
    more += len(paths) - len(named)
    if not named and not more:
        return None
    listed = ", ".join(named)
    if more:
        listed = f"{listed} and {more} more" if named else f"{more} files"
    return (
        "these files from before, left out because the whole set was "
        f"too large to carry (regenerate them if the task needs them): {listed}"
    )


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
    channel_id: str | None,
) -> UsageRecorder:
    """Meter the checkpoint turn to the tenant, at the OLD session's model.

    Shaped like `turn.prepare.bind_recorder` and for the same reason — a
    session runs the model it froze at create, so that is the model its work
    is priced at — but bound here rather than there because a transfer has no
    `TurnDeps` to hand, and because this turn's ledger rows carry
    `checkpoint_debit` so the spend is separable from turns a person asked
    for.
    """
    if (observation := current_outcome.get()) is not None:
        observation.model_by_session[ma_session_id] = model_id
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
        channel_id=channel_id,
    )


async def _poll_for_bundle(
    client: AsyncAnthropic,
    *,
    session_id: str,
    sleep: Callable[[float], Awaitable[None]],
    scope: Scope,
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
        records = await list_output_records(client, session_id, scope=scope)
        latest = next(
            (
                meta
                for meta in records
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
    scope: Scope,
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
        content = await read_output_record(client, bundle.id, scope=scope)
    except anthropic.APIStatusError as err:
        log.warning("workspace_transfer.download_failed", file_id=bundle.id, error=str(err)[:300])
        return "archive_missing"

    # Already gone (a concurrent sweep, a retried transfer) is not an error.
    with contextlib.suppress(anthropic.NotFoundError):
        await delete_output_record(client, bundle.id, scope=scope)

    try:

        async def body() -> AsyncIterator[bytes]:
            yield content

        uploaded = await legacy_call(
            managed_agents(client, scope=scope)
            .extension(Outputs, namespace="anthropic.outputs", version=1)
            .upload_bundle(
                scope,
                body(),
                filename=bundle.filename,
                media_type="application/gzip",
                key=token_hex(16),
            )
        )
    except anthropic.APIStatusError as err:
        log.warning("workspace_transfer.upload_failed", error=str(err)[:300])
        return "upload_failed"

    async with sessionmaker() as session, session.begin():
        await enqueue_pending_file_delete(
            session, file_id=uploaded, delete_after=now() + BUNDLE_RETENTION
        )
    return (uploaded, len(content))


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
    tool_safety: ToolSafetyPolicy,
    public_url: str | None,
    platform: str,
    reason: str = CHECKPOINT_REASON_CONFIGURATION_CHANGE,
    unsaved_work: UnsavedWorkChoice | None = None,
    max_bundle_bytes: int = HANDOFF_MAX_BYTES,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] = _utc_now,
    channel_id: str | None = None,
    before_send: Callable[[], Awaitable[None]] | None = None,
    account_id: uuid.UUID | None = None,
    fernet: MultiFernet | None = None,
    turn_path: Literal["legacy", "mux"] | None = None,
) -> TransferOutcome:
    """Walk the ladder for one replacement and report how far it got.

    Spends at most ONE billed turn, on the old session, and only when that
    session actually produced work (`is_worth_checkpointing`). Never
    archives or deletes the old session: retiring the mapping row is the
    caller's job, and the old session stays readable.

    `reason` is why the workspace is being replaced, in words, and reaches the
    old session in the checkpoint turn's controls.

    Nobody is watching the checkpoint turn, so it is unattended: right before
    its message the old session is gated from `tool_safety`
    (`gate_session_snapshot`), and every pause is decided by
    `unattended_posture`, never `AutoApprove`, before every send. The tool
    rules are those of the old session's own conversation channel on
    `platform` (`session_rules_channel`); `channel_id` is only who pays.

    `unsaved_work` is the person's answer about uncommitted changes in the
    mounted repository: `"leave"` keeps them in the old checkout and out of the
    bundle, anything else (including an unanswered None) captures them. The
    choice reaches the checkpoint prompt and, when it was `"leave"`, is also
    reported as something the successor did not get.

    `turn_path` is the turn path the checkpoint turn runs on, resolved by the
    caller; `None` reads settings in the driver.

    Never raises for an expected degradation — every rung below the top is a
    returned value. It does raise for an unexpected upstream failure (a 500
    on `events.list`, a DB error), because those are bugs or outages the
    caller's boundary should see, not states to paper over.
    """
    turn_scope = (
        Scope(
            tenant_id=str(tenant_id),
            account_id=str(account_id),
            principal_id="daimon",
            authorization_id="workspace-checkpoint",
        )
        if account_id is not None
        else None
    )
    scope = session_scope(
        tenant_id=tenant_id,
        account_id=account_id,
        call_site="workspace_transfer:transfer_workspace",
    )
    try:
        if turn_scope is None:
            events = await replay_events(client, session_id=old_session_id)
        else:
            events = await replay_events(client, session_id=old_session_id, scope=turn_scope)
    except anthropic.APIStatusError as err:
        if not _is_not_found(err):
            raise
        log.info("workspace_transfer.events_unavailable", session_id=old_session_id)
        return HistoryOnly(gap_reason="events_unavailable")

    turns = select_recent_turns(events)
    transcript = render_previous_session(turns, from_agent_name=from_agent_name) if turns else None

    from daimon.core.access_policy import agent_invoker_roles
    from daimon.core.authz import build_agent_ref
    from daimon.core.mux_compat import retrieve_agent
    from daimon.core.stores.access_policy import load_access_policy

    async with sessionmaker() as db:
        access = await load_access_policy(db, tenant_id=tenant_id)
    if any(rule.invoker_role_ids for rule in access.agent_rules.values()):
        try:
            predecessor = await retrieve_agent(client, old_snapshot.ma_agent_id, scope=scope)
        except anthropic.APIStatusError:
            # Unknown role requirements must never authorize a checkpoint send.
            log.warning("workspace_transfer.checkpoint_failed", session_id=old_session_id)
            return TranscriptOnly(transcript=transcript or "", gap_reason="checkpoint_failed")
        ref = build_agent_ref(predecessor.name, predecessor.metadata, old_snapshot.agent_name)
        if agent_invoker_roles(access, ref.names):
            log.info("workspace_transfer.skipped_checkpoint", session_id=old_session_id)
            return TranscriptOnly(transcript=transcript or "", gap_reason="invoker_not_allowed")

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
    try:
        old_session = await retrieve_session_record(client, old_session_id, scope=scope)
    except anthropic.APIStatusError as err:
        if not _is_not_found(err):
            raise
        log.warning("workspace_transfer.checkpoint_failed", session_id=old_session_id)
        return TranscriptOnly(transcript=transcript or "", gap_reason="session_dead")
    try:
        # A session the unattended gate cannot constrain (a coordinator roster,
        # a capability this release does not know) is never checkpointed: only
        # the conversation crosses, and no turn is spent on it.
        require_allowed_capabilities(
            old_session, allow_skills=ENTRY_POINT_POLICY["checkpoint"].allow_skills
        )
    except AdmissionDenied:
        log.warning(
            "workspace_transfer.checkpoint_failed",
            session_id=old_session_id,
            gap_reason="unattended_capabilities",
        )
        return TranscriptOnly(transcript=transcript or "", gap_reason="checkpoint_failed")

    # Unattended: the old session is gated before the message and before every
    # confirmation batch, each a single attempt.
    persistence_token = current_persistence.set(None)
    try:
        with unattended_sends(
            session_gate(
                client,
                old_session_id,
                tool_safety,
                scope=scope,
                public_url=public_url,
                session_factory=sessionmaker,
                tenant_id=tenant_id,
                entry_point="checkpoint",
            )
        ):
            state = await run_turn(
                anthropic=client,
                session_id=old_session_id,
                scope=turn_scope,
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
                        model_id=old_snapshot.pricing_id,
                        markup=markup,
                        channel_id=channel_id,
                    )
                ),
                tool_confirmation=unattended_posture(
                    tool_safety,
                    session=old_session,
                    fernet=fernet,
                    public_url=public_url,
                    session_factory=sessionmaker,
                    tenant_id=tenant_id,
                    channel_id=session_rules_channel(old_session, platform=platform),
                    entry_point="checkpoint",
                ),
                path=turn_path,
                deadline=checkpoint_deadline,
                # The checkpoint executes the old session: decided again right before.
                before_send=before_send,
            )
        failure = _checkpoint_gap_reason(state)
    finally:
        current_persistence.reset(persistence_token)

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

    bundle = await _poll_for_bundle(client, session_id=old_session_id, sleep=sleep, scope=scope)
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
            await delete_output_record(client, bundle.id, scope=scope)
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
    left_out_paths, left_out_more = checkpoint_left_out(reply)
    left_out = _left_out_phrase(left_out_paths, left_out_more)
    if left_out is not None:
        log.info(
            "workspace_transfer.files_left_out",
            session_id=old_session_id,
            count=len(left_out_paths) + left_out_more,
        )
        unpreserved = (*unpreserved, left_out)
    if first_head is not None and last_head is not None and first_head != last_head:
        log.warning(
            "workspace_transfer.repository_changed",
            session_id=old_session_id,
            head_before=first_head,
            head_after=last_head,
        )
        unpreserved = (*unpreserved, _COMMITTED_DURING_CHECKPOINT)

    rehosted = await _rehost_bundle(
        client,
        sessionmaker,
        bundle=bundle,
        now=now,
        scope=scope,
    )
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
    tool_safety: ToolSafetyPolicy
    """The deployment's policy; the checkpoint turn is unattended."""
    public_url: str | None
    platform: str
    """Where the old session's conversation is; its tool rules come from there."""
    channel_id: str | None = None
    """The channel the checkpoint turn's spend is attributed to (not its tool rules)."""
    from_agent_name: str | None = None
    """Override for the source agent's display name; defaults to the snapshot's."""
    fernet: MultiFernet | None = None
    account_id: uuid.UUID | None = None
    """The account from the caller's existing tenant authorization decision."""
    turn_path: Literal["legacy", "mux"] | None = None
    """The turn path the checkpoint turn runs on; `None` reads settings in the driver."""

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
        before_send: Callable[[], Awaitable[None]] | None = None,
    ) -> PreparedReplacement:
        from_agent_name = self.from_agent_name or old_snapshot.agent_name
        outcome = await transfer_workspace(
            self.anthropic,
            self.sessionmaker,
            old_session_id=old_session_id,
            old_snapshot=old_snapshot,
            tenant_id=self.tenant_id,
            account_id=self.account_id,
            external_user_id=self.external_user_id,
            transfer_id=transfer_id,
            markup=self.markup,
            checkpoint_deadline=deadline,
            from_agent_name=from_agent_name,
            tool_safety=self.tool_safety,
            fernet=self.fernet,
            public_url=self.public_url,
            platform=self.platform,
            reason=_checkpoint_reason(
                old_snapshot=old_snapshot,
                from_agent_name=from_agent_name,
                destination_model_id=destination_model_id,
                destination_agent_name=destination_agent_name,
            ),
            unsaved_work=unsaved_work,
            channel_id=self.channel_id,
            before_send=before_send,
            turn_path=self.turn_path,
        )
        prepared = as_prepared_replacement(
            outcome,
            destination_model_id=destination_model_id,
            from_agent_name=from_agent_name,
            to_agent_name=destination_agent_name,
            requested_work=requested_work,
        )
        return replace(
            prepared,
            extra_resources=restore_transfer_records(
                self.anthropic,
                old_session_id,
                scope=session_scope(
                    tenant_id=self.tenant_id,
                    account_id=self.account_id,
                    call_site="workspace_transfer:WorkspaceTransferRunner",
                ),
                inline=_inline_transfer(outcome),
            ),
        )


def _inline_transfer(outcome: TransferOutcome) -> ExtensionConfig:
    data: dict[str, JsonValue]
    if isinstance(outcome, FullHandoff):
        data = {
            "type": "full",
            "file_id": outcome.transfer_file_id,
            "mount_path": outcome.mount_path,
            "bytes_transferred": outcome.bytes_transferred,
            "transcript": outcome.transcript,
            "unpreserved": list(outcome.unpreserved),
        }
    elif isinstance(outcome, TranscriptOnly):
        data = {
            "type": "transcript",
            "transcript": outcome.transcript,
            "gap_reason": outcome.gap_reason,
        }
    else:
        data = {"type": "history", "gap_reason": outcome.gap_reason}
    return ExtensionConfig(
        namespace="anthropic.workspace_transfer",
        version=1,
        value={"outcome": data},
    )
