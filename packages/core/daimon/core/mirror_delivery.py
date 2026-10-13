"""Record conversation events for mirrors and drain the mirror outbox.

`record_conversation_event` runs where the turn ended (the MCP `ask`): it
stores the event once per matching mirror and nothing when none matches. The
chat adapter of the destination's platform drains the outbox with
`run_mirror_poller` and its own `MirrorPoster`, the same shape as routine
results (`daimon.core.routine_delivery`): the poster resolves the destination
on its platform, applies `mirror_refusal` with the live policy, and posts. A
refusal or a missing destination pauses the mirror, so a changed rule shows in
`list_mirrors` instead of failing quietly.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import MirrorsSettings
from daimon.core.mirror_events import ConversationEventV1
from daimon.core.mirrors import matching_mirrors
from daimon.core.stores import mirrors as mirrors_store
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.domain import MirrorDeliveryRow, MirrorRow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


async def record_conversation_event(
    sessionmaker: async_sessionmaker[AsyncSession], event: ConversationEventV1, *, now: datetime
) -> tuple[MirrorRow, ...]:
    """Queue `event` for every active mirror that covers it; returns those mirrors."""
    async with sessionmaker() as session, session.begin():
        mirrors = matching_mirrors(
            await mirrors_store.list_for_tenant(session, event.tenant_id, active_only=True), event
        )
        added = await mirrors_store.insert_deliveries(
            session, event=event, mirror_ids=[mirror.id for mirror in mirrors], now=now
        )
    if mirrors:
        log.info("mirror.event_enqueued", event_id=str(event.id), mirrors=len(mirrors), added=added)
    return mirrors


@dataclass(frozen=True)
class MirrorJob:
    """One claimed post with what the poster needs to decide and send it.
    `thread_id` is the session's thread from an earlier post, if any."""

    mirror: MirrorRow
    delivery: MirrorDeliveryRow
    event: ConversationEventV1
    policy: TenantAccessPolicy
    thread_id: str | None


@dataclass(frozen=True)
class MirrorOutcome:
    status: Literal["delivered", "skipped"]
    note: str | None = None
    dest_thread_id: str | None = None
    #: Set when the mirror itself can no longer post: it is paused with this reason.
    pause_reason: str | None = None


#: The adapter hook: resolve the destination, check it, post, say what happened.
MirrorPoster = Callable[[MirrorJob], Awaitable[MirrorOutcome]]


async def _prepare(
    sessionmaker: async_sessionmaker[AsyncSession], delivery: MirrorDeliveryRow
) -> MirrorJob | MirrorOutcome:
    """The job for a claimed post, or why it is skipped before the platform."""
    async with sessionmaker() as session:
        mirror = await mirrors_store.get_mirror(session, delivery.mirror_id)
        if mirror is None or mirror.status != "active":
            return MirrorOutcome(status="skipped", note="paused")
        event = delivery.payload
        if event is None:
            return MirrorOutcome(status="skipped", note="no_payload")
        if event.agent_id != mirror.source_agent_id:
            # The mirror was replaced with another agent after this was queued.
            return MirrorOutcome(status="skipped", note="replaced")
        if event.person_scoped and mirror.consent_basis is None:
            # The mirror was replaced without consent after this was queued.
            return MirrorOutcome(status="skipped", note="needs_consent")
        try:
            policy = await load_access_policy(session, tenant_id=mirror.tenant_id)
        except AccessPolicyUnreadable:
            return MirrorOutcome(status="skipped", note="access_policy_unreadable")
        thread_id = (
            await mirrors_store.get_session_thread(
                session, mirror_id=mirror.id, session_key=delivery.source_session_key
            )
            if mirror.dest_kind == "channel"
            else None
        )
    return MirrorJob(mirror, delivery, event, policy, thread_id)


async def poll_mirror_deliveries_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    post: MirrorPoster,
    settings: MirrorsSettings,
    now: datetime,
    owner: str | None = None,
) -> int:
    """Claim due posts for `platform`, post each, settle. Returns how many."""
    lease_owner = owner or f"{platform}:{uuid.uuid4()}"
    lease = timedelta(seconds=settings.lease_s)
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with sessionmaker() as session, session.begin():
        claimed = await mirrors_store.claim_mirror_deliveries(
            session,
            platform=platform,
            owner=lease_owner,
            now=now,
            lease=lease,
            max_pending_age=timedelta(seconds=settings.max_pending_age_s),
        )
    for delivery in claimed:
        prepared = await _prepare(sessionmaker, delivery)
        if isinstance(prepared, MirrorOutcome):
            outcome = prepared
        else:
            # Posts before this one may have outlived the lease, and another
            # poller may have taken the session's next post: never post late.
            async with sessionmaker() as session, session.begin():
                held = await mirrors_store.extend_mirror_claim(
                    session,
                    delivery.id,
                    owner=lease_owner,
                    now=now + timedelta(seconds=loop.time() - started),
                    lease=lease,
                )
            if not held:
                log.warning("mirror.claim_lost", delivery_id=str(delivery.id))
                continue
            try:
                outcome = await post(prepared)
            except Exception as err:
                # Named boundary: the poster is adapter code talking to a chat
                # API. Settling `skipped` keeps delivery at most once: the
                # failure may have come after a post landed.
                log.warning(
                    "mirror.delivery_failed",
                    delivery_id=str(delivery.id),
                    error_type=type(err).__name__,
                )
                outcome = MirrorOutcome(status="skipped", note="post_failed")
        settled_at = datetime.now(UTC)
        async with sessionmaker() as session, session.begin():
            settled = await mirrors_store.settle_mirror_delivery(
                session,
                delivery.id,
                owner=lease_owner,
                status=outcome.status,
                note=outcome.note,
                dest_thread_id=outcome.dest_thread_id,
                now=settled_at,
            )
            if outcome.pause_reason is not None:
                await mirrors_store.mark_paused(
                    session, delivery.mirror_id, reason=outcome.pause_reason, now=settled_at
                )
        if outcome.pause_reason is not None:
            log.warning(
                "mirror.paused", mirror_id=str(delivery.mirror_id), reason=outcome.pause_reason
            )
        log.info(
            "mirror.delivery_settled",
            delivery_id=str(delivery.id),
            status=outcome.status,
            note=outcome.note,
            settled=settled,
            latency_ms=int((settled_at - delivery.created_at).total_seconds() * 1000),
        )
    return len(claimed)


async def sweep_mirror_deliveries(
    sessionmaker: async_sessionmaker[AsyncSession], *, settings: MirrorsSettings, now: datetime
) -> int:
    """Settle queued posts that must not post, and drop their payload.

    Run by the scheduler whether or not mirrors or any poller are on, so a
    question queued before the feature was switched off, for a platform
    whose poller is off, or for an archived tenant is not kept.
    """
    async with sessionmaker() as session, session.begin():
        settled = await mirrors_store.settle_unpostable_deliveries(
            session, now=now, max_pending_age=timedelta(seconds=settings.max_pending_age_s)
        )
    if settled:
        log.info("mirror.deliveries_swept", settled=settled)
    return settled


async def run_mirror_poller(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    post: MirrorPoster,
    settings: MirrorsSettings,
    should_stop: Callable[[], bool],
) -> None:
    """Poll until `should_stop`, one `poll_mirror_deliveries_once` per interval."""
    owner = f"{platform}:{uuid.uuid4()}"
    while not should_stop():
        try:
            await poll_mirror_deliveries_once(
                sessionmaker,
                platform=platform,
                post=post,
                settings=settings,
                now=datetime.now(UTC),
                owner=owner,
            )
        except Exception:
            # Named boundary: a failed poll (database blip) must not end the
            # poller; the next interval retries.
            log.exception("mirror.delivery_poll_failed", platform=platform)
        await asyncio.sleep(settings.poll_interval_s)


__all__ = [
    "MirrorJob",
    "MirrorOutcome",
    "MirrorPoster",
    "poll_mirror_deliveries_once",
    "record_conversation_event",
    "run_mirror_poller",
    "sweep_mirror_deliveries",
]
