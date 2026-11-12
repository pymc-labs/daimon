"""The retention sweep: delete records older than their configured lifetime.

Lifetimes come from `Settings.retention` (0 keeps a record type forever), and a
conversation's from the `conversation_retention` feature, which a workspace may
shorten. Every delete runs in short batches, each its own transaction, so a
sweep is safe to stop and run again.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import structlog
from daimon.core.config import Settings
from daimon.core.features.registry import limit_int
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.stores import agent_posts, file_uploads, mux_erasure, retention, security_audit
from daimon.core.stores import tenants as tenants_store
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

BATCH = 500
# Files an MCP client or tool uploads are fetched within the hour; a day is ample.
UNOWNED_UPLOAD_LIFETIME = timedelta(hours=24)
# A sign-in record gets a day to be used before an unused one may go.
LOGIN_GRACE = timedelta(days=1)

Batch = Callable[..., Awaitable[int]]


async def _drain(sm: async_sessionmaker[AsyncSession], step: Batch, **kwargs: object) -> int:
    total = 0
    while True:
        async with sm() as session, session.begin():
            removed = await step(session, limit=BATCH, **kwargs)
        total += removed
        if removed < BATCH:
            return total


async def _aged(
    sm: async_sessionmaker[AsyncSession], event: str, days: int, now: datetime, step: Batch
) -> None:
    if days <= 0:
        return
    removed = await _drain(sm, step, before=now - timedelta(days=days))
    if removed:
        log.info(event, count=removed, days=days)


async def _security_audit(sm: async_sessionmaker[AsyncSession], days: int, now: datetime) -> None:
    if days <= 0:
        return
    cutoff = now - timedelta(days=days)
    events = posts = 0
    async with sm() as session:
        places = await retention.tenant_places(session)
    for tenant_id, _ in places:
        async with sm() as session, session.begin():
            events += await security_audit.prune_events(
                session, tenant_id=tenant_id, older_than=cutoff
            )
            posts += await agent_posts.prune_posts(session, tenant_id=tenant_id, older_than=cutoff)
    if events:
        log.info("security_audit_events.deleted", count=events, days=days)
    if posts:
        log.info("agent_posted_messages.deleted", count=posts, days=days)


async def conversation_days(
    session: AsyncSession, settings: Settings, tenant_id: uuid.UUID, platform: str
) -> int:
    """The workspace's `conversation_retention` in days; 0 keeps conversations."""
    place = FeaturePlace.of(tenant_id, platform, None)
    return limit_int((await feature_in(session, settings, place, "conversation_retention")).value)


async def _conversations(
    sm: async_sessionmaker[AsyncSession], settings: Settings, now: datetime
) -> None:
    mappings = events = outcomes = 0
    async with sm() as session:
        places = await retention.tenant_places(session)
    for tenant_id, platform in places:
        async with sm() as session:
            days = await conversation_days(session, settings, tenant_id, platform)
        if days <= 0:
            continue
        cutoff = now - timedelta(days=days)
        mappings += await _drain(
            sm, retention.delete_idle_thread_sessions, tenant_id=tenant_id, before=cutoff
        )
        while True:
            async with sm() as session, session.begin():
                erased = await mux_erasure.delete_idle_slots(
                    session, tenant_id=tenant_id, idle_before=cutoff, limit=BATCH
                )
            events += erased.journal_events
            if erased.slots < BATCH:
                break
        # After the conversations, whose existence keeps an outcome.
        outcomes += await _drain(
            sm, retention.delete_turn_outcomes, tenant_id=tenant_id, before=cutoff
        )
    if mappings:
        log.info("thread_sessions.deleted", count=mappings)
    if outcomes:
        log.info("turn_outcomes.deleted", count=outcomes)
    if events:
        log.info("journal.deleted", count=events)


async def _archived_tenants(sm: async_sessionmaker[AsyncSession], days: int, now: datetime) -> None:
    if days <= 0:
        return
    cutoff = now - timedelta(days=days)
    async with sm() as session:
        candidates = await tenants_store.list_expired_archived_tenants(
            session, archived_before=cutoff, limit=BATCH
        )
    deleted = 0
    for tenant_id in candidates:
        async with sm() as session, session.begin():
            deleted += await tenants_store.delete_archived_tenant(
                session, tenant_id=tenant_id, archived_before=cutoff
            )
    if deleted:
        log.info("tenants.deleted", count=deleted, days=days)


async def sweep(sm: async_sessionmaker[AsyncSession], settings: Settings, *, now: datetime) -> None:
    """Run every retention rule once. A failing rule raises; earlier ones stay done."""
    keep = settings.retention
    await _security_audit(sm, keep.security_audit_days, now)
    rules: list[tuple[str, int, Batch]] = [
        ("web_audit.deleted", keep.admin_history_days, retention.delete_web_audit),
        ("channel_changes.deleted", keep.admin_history_days, retention.delete_channel_changes),
        (
            "task_continuations.deleted",
            keep.task_continuations_days,
            retention.delete_task_continuations,
        ),
        ("long_jobs.deleted", keep.job_output_days, retention.delete_long_jobs),
        ("compute_output.cleared", keep.job_output_days, retention.clear_compute_output),
        ("routine_results.cleared", keep.job_output_days, retention.clear_routine_results),
        (
            "credential_requests.deleted",
            keep.access_requests_days,
            retention.delete_credential_requests,
        ),
        (
            "github_access_requests.deleted",
            keep.access_requests_days,
            retention.delete_github_access_requests,
        ),
        ("web_threads.deleted", keep.deleted_threads_days, retention.delete_deleted_web_threads),
        ("message_feedback.deleted", keep.feedback_days, retention.delete_feedback),
        (
            "support_escalations.deleted",
            keep.feedback_days,
            retention.delete_support_escalations,
        ),
        ("channel_names.deleted", keep.channel_names_days, retention.delete_channel_names),
    ]
    for event, days, step in rules:
        await _aged(sm, event, days, now, step)
    await _conversations(sm, settings, now)
    uploads = await _drain(
        sm,
        file_uploads.delete_stale_unowned_uploads,
        created_before=now - UNOWNED_UPLOAD_LIFETIME,
    )
    if uploads:
        log.info("file_uploads.deleted", count=uploads)
    logins = await _drain(sm, retention.delete_unreferenced_logins, before=now - LOGIN_GRACE)
    if logins:
        log.info("web_logins.deleted", count=logins)
    await _archived_tenants(sm, keep.archived_tenants_days, now)
