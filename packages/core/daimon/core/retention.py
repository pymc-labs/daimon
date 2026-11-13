"""The retention sweep: delete records older than their configured lifetime.

Lifetimes come from `Settings.retention` (0 keeps a record type forever), and a
conversation's from the `conversation_retention` feature, which a workspace may
shorten. Every delete runs in short batches, each its own transaction, so a
sweep is safe to stop and run again.

Each rule is claimed in the database (`retention_runs`) and runs on its own: it is
due once a day whichever process asks, a restart does not rerun it, and a rule that
fails is logged and retried later while the others go on.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import structlog
from daimon.core.config import Settings
from daimon.core.features.registry import limit_int
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.stores import (
    agent_posts,
    file_uploads,
    mux_erasure,
    retention,
    security_audit,
    tenant_retirement,
)
from daimon.core.stores import tenants as tenants_store
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

BATCH = 500
# A rule is due once a day, and tried again after a failure or an interrupted run.
RERUN_EVERY = timedelta(hours=24)
RETRY_AFTER = timedelta(minutes=30)
# Files an MCP client or tool uploads are fetched within the hour; a day is ample.
UNOWNED_UPLOAD_LIFETIME = timedelta(hours=24)
# A sign-in record gets a day to be used before an unused one may go.
LOGIN_GRACE = timedelta(days=1)

Batch = Callable[..., Awaitable[int]]
Run = Callable[[async_sessionmaker[AsyncSession], Settings, datetime], Awaitable[None]]


async def _drain(sm: async_sessionmaker[AsyncSession], step: Batch, **kwargs: object) -> int:
    """Run `step` in short transactions until a batch comes back short.

    The position carries from batch to batch, so rows a rule keeps are read once.
    """
    total = 0
    position = retention.Position()
    while True:
        async with sm() as session, session.begin():
            removed = await step(session, limit=BATCH, cursor=position, **kwargs)
        total += removed
        if removed < BATCH:
            return total


class Incomplete(Exception):
    """Part of a rule failed; it is logged where it failed and the rule is retried."""


async def _each_tenant(
    sm: async_sessionmaker[AsyncSession],
    work: Callable[[uuid.UUID, str], Awaitable[None]],
) -> None:
    """Run `work` for every tenant; one tenant's failure does not stop the rest."""
    async with sm() as session:
        places = await retention.tenant_places(session)
    failed = 0
    for tenant_id, platform in places:
        try:
            await work(tenant_id, platform)
        except Exception:
            failed += 1
            log.exception("retention.tenant_failed", tenant_id=str(tenant_id))
    if failed:
        raise Incomplete(f"{failed} tenants failed")


async def _aged(
    sm: async_sessionmaker[AsyncSession], event: str, days: int, now: datetime, step: Batch
) -> None:
    removed = await _drain(sm, step, before=now - timedelta(days=days))
    if removed:
        log.info(event, count=removed, days=days)


async def _security_audit(sm: async_sessionmaker[AsyncSession], days: int, now: datetime) -> None:
    cutoff = now - timedelta(days=days)
    totals: Counter[str] = Counter()

    async def tenant(tenant_id: uuid.UUID, _platform: str) -> None:
        for event, prune in (
            ("security_audit_events.deleted", security_audit.prune_events),
            ("agent_posted_messages.deleted", agent_posts.prune_posts),
        ):
            while True:
                async with sm() as session, session.begin():
                    removed = await prune(
                        session, tenant_id=tenant_id, older_than=cutoff, limit=BATCH
                    )
                totals[event] += removed
                if removed < BATCH:
                    break

    try:
        await _each_tenant(sm, tenant)
    finally:
        for event, count in totals.items():
            if count:
                log.info(event, count=count, days=days)


async def conversation_days(
    session: AsyncSession, settings: Settings, tenant_id: uuid.UUID, platform: str
) -> int:
    """The workspace's `conversation_retention` in days; 0 keeps conversations."""
    place = FeaturePlace.of(tenant_id, platform, None)
    return limit_int((await feature_in(session, settings, place, "conversation_retention")).value)


async def _each_conversation_cutoff(
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    now: datetime,
    work: Callable[[uuid.UUID, datetime], Awaitable[None]],
) -> None:
    """Run `work` with each tenant's conversation cutoff, skipping tenants that keep them."""

    async def tenant(tenant_id: uuid.UUID, platform: str) -> None:
        async with sm() as session:
            days = await conversation_days(session, settings, tenant_id, platform)
        if days > 0:
            await work(tenant_id, now - timedelta(days=days))

    await _each_tenant(sm, tenant)


async def _conversations(
    sm: async_sessionmaker[AsyncSession], settings: Settings, now: datetime
) -> None:
    totals: Counter[str] = Counter()

    async def tenant(tenant_id: uuid.UUID, cutoff: datetime) -> None:
        totals["thread_sessions.deleted"] += await _drain(
            sm, retention.delete_idle_thread_sessions, tenant_id=tenant_id, before=cutoff
        )
        while True:
            async with sm() as session, session.begin():
                erased = await mux_erasure.delete_idle_slots(
                    session, tenant_id=tenant_id, idle_before=cutoff, limit=BATCH
                )
            totals["journal.deleted"] += erased.journal_events
            if erased.slots < BATCH:
                break
        # After the conversations, whose existence keeps an outcome.
        totals["turn_outcomes.deleted"] += await _drain(
            sm, retention.delete_turn_outcomes, tenant_id=tenant_id, before=cutoff
        )

    try:
        await _each_conversation_cutoff(sm, settings, now, tenant)
    finally:
        for event, count in totals.items():
            if count:
                log.info(event, count=count)


async def _archived_tenants(sm: async_sessionmaker[AsyncSession], days: int, now: datetime) -> None:
    cutoff = now - timedelta(days=days)
    async with sm() as session:
        candidates = await tenants_store.list_expired_archived_tenants(
            session, archived_before=cutoff, limit=BATCH
        )
    retired = failed = 0
    for tenant_id in candidates:
        try:
            async with sm() as session, session.begin():
                retired += await tenant_retirement.retire_archived_tenant(
                    session, tenant_id=tenant_id, archived_before=cutoff
                )
        except Exception:
            failed += 1
            log.exception("retention.tenant_failed", tenant_id=str(tenant_id))
    if retired:
        log.info("tenants.retired", count=retired, days=days)
    if failed:
        raise Incomplete(f"{failed} tenants failed")


async def _uploads(sm: async_sessionmaker[AsyncSession], now: datetime) -> None:
    removed = await _drain(
        sm, file_uploads.delete_stale_unowned_uploads, created_before=now - UNOWNED_UPLOAD_LIFETIME
    )
    if removed:
        log.info("file_uploads.deleted", count=removed)


async def _logins(sm: async_sessionmaker[AsyncSession], now: datetime) -> None:
    removed = await _drain(sm, retention.delete_unreferenced_logins, before=now - LOGIN_GRACE)
    if removed:
        log.info("web_logins.deleted", count=removed)


def _aged_rule(event: str, days: int, step: Batch) -> Run:
    async def run(sm: async_sessionmaker[AsyncSession], _: Settings, now: datetime) -> None:
        verb = "cleared" if step.__name__.startswith("clear_") else "deleted"
        await _aged(sm, f"{event}.{verb}", days, now, step)

    return run


def rules(settings: Settings) -> list[tuple[str, int, Run]]:
    """Every rule as (name, days, run); `days` 0 means the operator keeps that record."""
    keep = settings.retention
    aged: list[tuple[str, int, Batch]] = [
        ("web_audit", keep.admin_history_days, retention.delete_web_audit),
        ("channel_changes", keep.admin_history_days, retention.delete_channel_changes),
        ("task_continuations", keep.task_continuations_days, retention.delete_task_continuations),
        ("long_jobs", keep.job_output_days, retention.delete_long_jobs),
        ("compute_output", keep.job_output_days, retention.clear_compute_output),
        ("routine_results", keep.job_output_days, retention.clear_routine_results),
        ("credential_requests", keep.access_requests_days, retention.delete_credential_requests),
        (
            "github_access_requests",
            keep.access_requests_days,
            retention.delete_github_access_requests,
        ),
        ("web_threads", keep.deleted_threads_days, retention.delete_deleted_web_threads),
        ("message_feedback", keep.feedback_days, retention.delete_feedback),
        ("support_escalations", keep.feedback_days, retention.delete_support_escalations),
        ("channel_names", keep.channel_names_days, retention.delete_channel_names),
    ]

    async def security(sm: async_sessionmaker[AsyncSession], _: Settings, now: datetime) -> None:
        await _security_audit(sm, keep.security_audit_days, now)

    async def archived(sm: async_sessionmaker[AsyncSession], _: Settings, now: datetime) -> None:
        await _archived_tenants(sm, keep.archived_tenants_days, now)

    async def uploads(sm: async_sessionmaker[AsyncSession], _: Settings, now: datetime) -> None:
        await _uploads(sm, now)

    async def logins(sm: async_sessionmaker[AsyncSession], _: Settings, now: datetime) -> None:
        await _logins(sm, now)

    return [
        ("security_audit", keep.security_audit_days, security),
        *((name, days, _aged_rule(name, days, step)) for name, days, step in aged),
        ("conversations", 1, _conversations),
        ("file_uploads", 1, uploads),
        ("web_logins", 1, logins),
        ("archived_tenants", keep.archived_tenants_days, archived),
    ]


async def _run_rule(
    sm: async_sessionmaker[AsyncSession], settings: Settings, name: str, run: Run, now: datetime
) -> None:
    async with sm() as session, session.begin():
        claimed = await retention.claim_rule(
            session, rule=name, now=now, every=RERUN_EVERY, retry=RETRY_AFTER
        )
    if not claimed:
        return
    try:
        await run(sm, settings, now)
    except Incomplete as incomplete:
        log.warning("retention.rule_incomplete", rule=name, reason=str(incomplete))
        return
    except Exception:
        log.exception("retention.rule_failed", rule=name)
        return
    async with sm() as session, session.begin():
        await retention.finish_rule(session, rule=name, now=now)


async def sweep(sm: async_sessionmaker[AsyncSession], settings: Settings, *, now: datetime) -> None:
    """Run each retention rule that is due, one at a time.

    A rule is due once a day (`retention_runs`). One that fails is logged and tried
    again after `RETRY_AFTER`; the rest run either way.
    """
    for name, days, run in rules(settings):
        if days > 0:
            await _run_rule(sm, settings, name, run, now)
