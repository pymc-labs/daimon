"""Durable ownership, activity and unsettled usage for the scoped backfill."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from daimon.core._models import (
    GitHubAppSessionVault,
    Tenant,
    ThreadSession,
    TurnOutcome,
    UsageEvent,
    UsageSweepSession,
)
from sqlalchemy import and_, case, func, literal, or_, select, union_all, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

RECENT_WINDOW = timedelta(hours=2)


@dataclass(frozen=True)
class SweepCandidate:
    session_id: str
    tenant_id: UUID
    activity_at: datetime
    needs_archive: bool


async def register(
    session: AsyncSession, *, session_id: str, tenant_id: UUID, resumable: bool = True
) -> None:
    """Register before a message can incur usage; wake/protect a continued session.

    Internal API-only credentials can name a tenant absent from this database.
    INSERT SELECT excludes those sessions, which this deployment cannot bill.
    """
    stmt = insert(UsageSweepSession).from_select(
        ["session_id", "tenant_id", "resumable"],
        select(literal(session_id), Tenant.id, literal(resumable)).where(Tenant.id == tenant_id),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[UsageSweepSession.session_id],
            set_={
                "updated_at": func.now(),
                "finished_at": None,
                "unsettled": True,
                "resumable": UsageSweepSession.resumable | resumable,
            },
            where=UsageSweepSession.tenant_id == tenant_id,
        )
    )


async def list_candidates(session: AsyncSession, *, now: datetime) -> list[SweepCandidate]:
    """Select recent activity or durable unsettled usage; never page old history.

    Import recent legacy records and explicit failed/unobserved turns. Once
    reconciled, an unchanged source cannot set its settled flag back to true.
    """
    cutoff = now - RECENT_WINDOW
    sources = union_all(
        select(
            ThreadSession.ma_session_id.label("id"),
            ThreadSession.tenant_id.label("tenant"),
            ThreadSession.updated_at.label("touched"),
        ).where(
            or_(
                ThreadSession.updated_at >= cutoff,
                ThreadSession.active_turn_message_id.is_not(None),
            )
        ),
        select(TurnOutcome.session_id, TurnOutcome.tenant_id, TurnOutcome.ended_at).where(
            TurnOutcome.session_id.is_not(None),
            TurnOutcome.tenant_id.is_not(None),
            or_(
                TurnOutcome.ended_at >= cutoff,
                TurnOutcome.reason != "completed",
                and_(
                    TurnOutcome.platform == "mcp",
                    TurnOutcome.model_calls.is_(None),
                    ~select(UsageEvent.id)
                    .where(
                        UsageEvent.managed_session_id == TurnOutcome.session_id,
                        UsageEvent.occurred_at >= TurnOutcome.ended_at,
                    )
                    .exists(),
                ),
            ),
        ),
        select(UsageEvent.managed_session_id, UsageEvent.tenant_id, UsageEvent.occurred_at).where(
            UsageEvent.occurred_at >= cutoff,
            # Direct tool/model charges use synthetic ledger session IDs,
            # not remote MA sessions. Never spend a request retrieving them.
            ~UsageEvent.managed_session_id.startswith("classifier:"),
            ~UsageEvent.managed_session_id.startswith("gemini:"),
            ~UsageEvent.managed_session_id.startswith("thread-naming:"),
        ),
        select(
            GitHubAppSessionVault.session_id,
            GitHubAppSessionVault.tenant_id,
            GitHubAppSessionVault.last_started_at,
        ).where(
            or_(
                GitHubAppSessionVault.last_started_at >= cutoff,
                and_(
                    GitHubAppSessionVault.is_mcp.is_(True),
                    GitHubAppSessionVault.closed_at.is_(None),
                ),
            )
        ),
    ).subquery()
    owned = select(sources.c.id, sources.c.tenant, func.max(sources.c.touched)).group_by(
        sources.c.id, sources.c.tenant
    )
    stmt = insert(UsageSweepSession).from_select(["session_id", "tenant_id", "updated_at"], owned)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[UsageSweepSession.session_id],
            set_={"updated_at": stmt.excluded.updated_at, "unsettled": True},
            where=and_(
                UsageSweepSession.tenant_id == stmt.excluded.tenant_id,
                stmt.excluded.updated_at > UsageSweepSession.updated_at,
            ),
        )
    )
    in_flight = UsageSweepSession.session_id.in_(
        select(ThreadSession.ma_session_id).where(ThreadSession.active_turn_message_id.is_not(None))
    )
    active = or_(
        UsageSweepSession.unsettled.is_(True),
        in_flight,
        and_(
            UsageSweepSession.updated_at >= cutoff,
            UsageSweepSession.last_swept_at <= now - timedelta(minutes=30),
        ),
    )
    archive = and_(
        UsageSweepSession.resumable.is_(False),
        UsageSweepSession.finished_at < cutoff,
        UsageSweepSession.updated_at < cutoff,
        UsageSweepSession.last_swept_at.is_not(None),
        UsageSweepSession.remote_status.in_(["idle", "terminated"]),
        ~UsageSweepSession.session_id.in_(
            select(ThreadSession.ma_session_id).where(
                or_(
                    ThreadSession.status == "live",
                    ThreadSession.active_turn_message_id.is_not(None),
                )
            )
        ),
        ~UsageSweepSession.session_id.in_(
            select(GitHubAppSessionVault.session_id).where(
                GitHubAppSessionVault.is_mcp.is_(True), GitHubAppSessionVault.closed_at.is_(None)
            )
        ),
    )
    rows = await session.execute(
        select(
            UsageSweepSession.session_id,
            UsageSweepSession.tenant_id,
            UsageSweepSession.updated_at,
            archive,
        )
        .where(UsageSweepSession.archived_at.is_(None), or_(active, archive))
        .order_by(
            UsageSweepSession.unsettled.desc(),
            UsageSweepSession.last_swept_at.asc().nulls_first(),
            UsageSweepSession.session_id,
        )
        .limit(2)
    )
    return [
        SweepCandidate(sid, tenant_id, activity_at, bool(needs_archive))
        for sid, tenant_id, activity_at, needs_archive in rows
    ]


async def mark_swept(
    session: AsyncSession,
    *,
    session_id: str,
    started_at: datetime,
    activity_at: datetime,
    status: str,
    archived_at: datetime | None = None,
    usage_complete: bool = True,
) -> None:
    """Settle only after complete replay of the selected activity snapshot.

    Comparing snapshots also protects a send transaction begun before the
    pass: its SQL now() can precede started_at despite committing later.
    """
    await session.execute(
        update(UsageSweepSession)
        .where(UsageSweepSession.session_id == session_id)
        .values(
            last_swept_at=started_at,
            remote_status=status,
            archived_at=archived_at,
            unsettled=case(
                (UsageSweepSession.updated_at != activity_at, True),
                else_=not usage_complete
                or (status in ("running", "rescheduling") and archived_at is None),
            ),
        )
    )


async def can_archive(session: AsyncSession, *, session_id: str, now: datetime) -> bool:
    """Called under the send fence; a resumed handle/live thread always wins."""
    live_thread = (
        select(ThreadSession.id)
        .where(
            ThreadSession.ma_session_id == session_id,
            or_(ThreadSession.status == "live", ThreadSession.active_turn_message_id.is_not(None)),
        )
        .exists()
    )
    open_mcp = (
        select(GitHubAppSessionVault.session_id)
        .where(
            GitHubAppSessionVault.session_id == session_id,
            GitHubAppSessionVault.is_mcp.is_(True),
            GitHubAppSessionVault.closed_at.is_(None),
        )
        .exists()
    )
    return bool(
        await session.scalar(
            select(UsageSweepSession.session_id).where(
                UsageSweepSession.session_id == session_id,
                UsageSweepSession.resumable.is_(False),
                UsageSweepSession.finished_at < now - RECENT_WINDOW,
                UsageSweepSession.unsettled.is_(False),
                UsageSweepSession.archived_at.is_(None),
                UsageSweepSession.updated_at < now - RECENT_WINDOW,
                ~live_thread,
                ~open_mcp,
            )
        )
    )


async def mark_archived(session: AsyncSession, *, session_id: str, now: datetime) -> None:
    await session.execute(
        update(UsageSweepSession)
        .where(UsageSweepSession.session_id == session_id)
        .values(archived_at=now, unsettled=False)
    )


async def mark_finished(session: AsyncSession, *, session_id: str) -> None:
    """Only a terminal headless runner can release its session for later cleanup."""
    await session.execute(
        update(UsageSweepSession)
        .where(UsageSweepSession.session_id == session_id)
        .values(finished_at=func.now())
    )
