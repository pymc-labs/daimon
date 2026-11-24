"""One claim, post, settle loop for every post Daimon makes on its own schedule.

Two sources feed it: routine results (the outbox on the routine row, unchanged) and
workflow posts (`workflow_deliveries`: Finish results, post steps, asks, notices). Each chat
adapter runs `run_poller` with one `JobPoster`, its routine poster and, where it has
one, its workflow poster. A source with no poster is left pending, the safe default for
a platform that cannot post it yet.

Delivery is at most once: claim (with a lease), post, settle. A claim whose lease ran
out is settled `skipped/interrupted` and never handed out again; a post that raised
settles too, because it may have landed.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

import structlog
from daimon.core.ops_events import OpsEvent, report_ops_event
from daimon.core.routine_files import (
    PREFIX,
    RoutineFile,
    RoutinePayload,
    decode_payload,
    payload_for,
)
from daimon.core.stores import workflow_deliveries
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import claim_routine_deliveries, settle_routine_delivery
from daimon.core.stores.workflow_deliveries import DeliveryRow, SettledStatus
from daimon.core.stores.workflow_run_events import NewEvent, append_events
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.owner_notices import OwnerDm

__all__ = [
    "DELIVERY_LEASE",
    "DELIVERY_POLL_INTERVAL_S",
    "DeliveryOutcome",
    "JobPoster",
    "RoutinePoster",
    "WorkflowPoster",
    "files_envelope",
    "job_payload",
    "poll_once",
    "run_poller",
]

log = structlog.get_logger(__name__)

DELIVERY_LEASE: Final[timedelta] = timedelta(minutes=2)
DELIVERY_POLL_INTERVAL_S: Final[float] = 15.0
#: How often run cards are looked at; each is edited at most every `chat_cards.EDIT_EVERY`.
RUN_CARD_INTERVAL_S: Final[float] = 3.0
FILES_FAILED: Final[str] = "File delivery failed or was interrupted; it will not be retried."

#: Skip notes that mean a rule refused the post, recorded `refused` on a workflow post.
REFUSALS: Final[frozenset[str]] = frozenset(
    {
        "protected_channel",
        "creator_cannot_post",
        "invoker_not_allowed",
        "access_policy_unreadable",
        "audience_contained",
        "audience_unreadable",
        "reach",
        "speaker_not_on_platform",
        "dm_disabled",
        "not_a_destination",
    }
)

#: Routine skip notes that mean a post was attempted and failed; every other skip is deliberate.
_POST_FAILURE_NOTES: Final[frozenset[str]] = frozenset(
    {
        "post_failed",
        "Delivery was interrupted; it will not be retried.",
        "The DM could not be delivered; it will not be retried.",
        "access_policy_unreadable",
        "The server access settings could not be read.",
    }
)


@dataclass(frozen=True)
class DeliveryOutcome:
    status: Literal["delivered", "skipped"]
    #: A `SkipReason`, or `dm_fallback:<reason>` for a result delivered to the
    #: creator by direct message instead of the destination.
    note: str | None = None
    #: A workflow post's landing: `channel`, `thread`, `message`, `link`, and `opened` when
    #: the post opened (or found by title) the thread it went to.
    placed: dict[str, str | bool | None] | None = None
    #: The error class of a post that was tried and failed, such as a fallback DM.
    failed: str | None = None


@dataclass(frozen=True)
class Claim:
    """One claimed post: a routine's pending result, or a queued workflow post."""

    routine: RoutineRow | None = None
    workflow: DeliveryRow | None = None

    @property
    def payload(self) -> RoutinePayload:
        if self.routine is not None:
            return decode_payload(payload_for(self.routine))
        assert self.workflow is not None
        return job_payload(self.workflow)

    @property
    def log_fields(self) -> dict[str, str]:
        if self.routine is not None:
            return {"tenant_id": str(self.routine.tenant_id), "routine_id": str(self.routine.id)}
        assert self.workflow is not None
        return {"delivery_id": str(self.workflow.id), "workflow_id": str(self.workflow.workflow_id)}

    @property
    def event_prefix(self) -> str:
        return "routine" if self.routine is not None else "workflow"


def job_payload(row: DeliveryRow) -> RoutinePayload:
    """A workflow post's text and file refs. Refs that do not parse are dropped."""
    files: list[RoutineFile] = []
    raw_files: list[object] = row.payload.get("files") or []
    for raw in raw_files:
        try:
            files.append(RoutineFile.model_validate(raw))
        except ValueError:
            log.warning("workflow.delivery_file_unreadable", delivery_id=str(row.id))
    return RoutinePayload(text=str(row.payload.get("text") or ""), files=files[:10])


def files_envelope(row: DeliveryRow) -> str:
    """The file-only envelope `deliver_routine_files` reads for a workflow post."""
    return PREFIX + RoutinePayload(text="", files=job_payload(row).files).model_dump_json()


#: The adapter hook for a routine: post `row`'s result (or refuse) and say what happened.
#: `fire.delivery.routine_poster` builds it over the platform's `Poster` of `DeliveryJob`s.
RoutinePoster = Callable[[RoutineRow], Awaitable[DeliveryOutcome]]
#: The adapter hook for a workflow post (`daimon.core.workflows.delivery` clears it).
WorkflowPoster = Callable[[DeliveryRow], Awaitable[DeliveryOutcome]]


@dataclass(frozen=True)
class JobPoster:
    """An adapter's posters for both sources. Without `workflow`, workflow posts stay pending."""

    routine: RoutinePoster
    workflow: WorkflowPoster | None = None
    #: One round of editing chat-started runs' cards (`workflows.chat_cards.follower`).
    run_cards: Callable[[], Awaitable[int]] | None = None

    async def post_job(self, job: Claim) -> DeliveryOutcome:
        if job.routine is not None:
            return await self.routine(job.routine)
        assert job.workflow is not None and self.workflow is not None
        return await self.workflow(job.workflow)


def _settled_status(outcome: DeliveryOutcome) -> SettledStatus:
    if outcome.status == "delivered":
        return "sent"
    if outcome.note in REFUSALS:
        return "refused"
    return "failed" if outcome.note in ("post_failed", FILES_FAILED) else "skipped"


async def _settle(
    session: AsyncSession, job: Claim, *, owner: str, outcome: DeliveryOutcome, now: datetime
) -> bool:
    if job.routine is not None:
        return await settle_routine_delivery(
            session, job.routine.id, owner=owner, status=outcome.status, note=outcome.note, now=now
        )
    assert job.workflow is not None
    status = _settled_status(outcome)
    settled = await workflow_deliveries.settle_delivery(
        session,
        job.workflow.id,
        owner=owner,
        status=status,
        now=now,
        note=outcome.note,
        placed=outcome.placed if status == "sent" else None,
    )
    if settled is not None and settled.run_id is not None:
        kind = {"sent": "sent", "refused": "refused"}.get(status, "skipped")
        await append_events(
            session,
            run_id=settled.run_id,
            events=[
                NewEvent(
                    f"delivery.{kind}",
                    None,
                    {
                        "delivery_id": str(settled.id),
                        "kind": settled.kind,
                        "status": status,
                        "note": outcome.note,
                        "link": (settled.placed or {}).get("link"),
                    },
                )
            ],
            actor="delivery",
            at=now,
        )
    if settled is not None and settled.kind == "ask" and status != "sent":
        # Lazy: the asks module reaches this one through the routine delivery rules.
        from daimon.core.workflows.asks import tell_owner_undelivered

        await tell_owner_undelivered(session, settled, outcome.note)
    return settled is not None


async def poll_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    post: JobPoster,
    now: datetime,
    owner: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Claim due posts for `platform` from both sources, post each, settle. Returns how many."""
    lease_owner = owner or f"{platform}:{uuid.uuid4()}"
    async with sessionmaker() as session, session.begin():
        jobs = [
            Claim(routine=row)
            for row in await claim_routine_deliveries(
                session, platform=platform, owner=lease_owner, now=now, lease=DELIVERY_LEASE
            )
        ]
        if post.workflow is not None:
            jobs += [
                Claim(workflow=row)
                for row in await workflow_deliveries.claim_deliveries(
                    session, platform=platform, owner=lease_owner, now=now, lease=DELIVERY_LEASE
                )
            ]
    for job in jobs:
        payload = job.payload
        raised = False
        if not (payload.text.strip() or payload.files):
            outcome = DeliveryOutcome(status="skipped", note="no_result")
        else:
            try:
                outcome = await post.post_job(job)
            except Exception as err:
                # Named boundary: the poster is adapter code talking to a chat API.
                # Settling (not back to pending) keeps delivery at most once: a
                # failure may have landed after the post.
                raised = True
                log.warning(
                    f"{job.event_prefix}.delivery_failed",
                    **job.log_fields,
                    status="post_failed",
                    error_type=type(err).__name__,
                )
                if job.routine is not None:
                    _report_routine_failed(sessionmaker, job.routine, platform, type(err).__name__)
                outcome = DeliveryOutcome(
                    status="skipped", note=FILES_FAILED if payload.files else "post_failed"
                )
        async with sessionmaker() as session, session.begin():
            settled = await _settle(
                session,
                job,
                owner=lease_owner,
                outcome=outcome,
                now=datetime.now(UTC) if clock is None else clock(),
            )
        log.info(
            f"{job.event_prefix}.delivery_settled",
            **job.log_fields,
            status=outcome.status,
            note=outcome.note,
            settled=settled,
        )
        if job.routine is not None and outcome.status == "skipped" and not raised:
            _log_routine_skip(sessionmaker, job.routine, platform, outcome)
    return len(jobs)


def _log_routine_skip(
    sm: async_sessionmaker[AsyncSession], row: RoutineRow, platform: str, outcome: DeliveryOutcome
) -> None:
    # Real post failures page; deliberate skips (policy, archive, ...) only log.
    if outcome.note == "no_result":
        return
    failed = outcome.failed is not None or outcome.note in _POST_FAILURE_NOTES
    (log.warning if failed else log.info)(
        "routine.delivery_failed" if failed else "routine.delivery_skipped",
        tenant_id=str(row.tenant_id),
        routine_id=str(row.id),
        status="skipped",
        note=outcome.note,
    )
    if failed:
        _report_routine_failed(sm, row, platform, outcome.failed or "post_failed")


def _report_routine_failed(
    sm: async_sessionmaker[AsyncSession], row: RoutineRow, platform: str, reason: str
) -> None:
    report_ops_event(
        sm,
        OpsEvent(
            "routine.delivery_failed",
            row.tenant_id,
            reason=reason,
            routine_id=row.id,
            agent_id=row.agent_id,
            platform=platform,
        ),
        logged=False,
    )


async def run_poller(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    post: JobPoster,
    should_stop: Callable[[], bool],
    interval_s: float = DELIVERY_POLL_INTERVAL_S,
    owner_dm: OwnerDm | None = None,
) -> None:
    """Poll until `should_stop`, one `poll_once` per interval; run cards on their own beat.
    With `owner_dm`, also send the failure notices waiting for this platform's routine owners."""
    from daimon.core.owner_notices import poll_owner_notices_once  # a cycle at import time

    owner = f"{platform}:{uuid.uuid4()}"
    cards = (
        None
        if post.run_cards is None
        else asyncio.create_task(_follow(post.run_cards, platform, should_stop))
    )
    try:
        while not should_stop():
            try:
                await poll_once(
                    sessionmaker, platform=platform, post=post, now=datetime.now(UTC), owner=owner
                )
                if owner_dm is not None:
                    await poll_owner_notices_once(sessionmaker, platform=platform, send=owner_dm)
            except Exception:
                log.exception("routine.delivery_poll_failed", platform=platform)
            await asyncio.sleep(interval_s)
    finally:
        if cards is not None:
            cards.cancel()


async def _follow(
    once: Callable[[], Awaitable[int]], platform: str, should_stop: Callable[[], bool]
) -> None:
    while not should_stop():
        try:
            await once()
        except Exception:
            log.exception("workflow.run_cards_failed", platform=platform)
        await asyncio.sleep(RUN_CARD_INTERVAL_S)
