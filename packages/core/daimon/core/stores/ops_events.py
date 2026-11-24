"""Ops events and the resolve or mute state of their issues.

Every function takes a required `tenant_id`, so a read or write is never wider than one
workspace; the caller takes it from the signed-in person, the operator token or the CLI's
required argument. An issue is the events of one fingerprint, grouped here; the events
within a window are counted, and first and last seen are over the issue's whole life.
A person's rows go with their account (`delete_for_account`), every row with its tenant,
and `prune_ops_events` ends retention.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple

from daimon.core._models import OpsEvent, OpsIssueState, Routine, Watch
from daimon.core.cursor import Cursor
from daimon.core.ops_catalog import OpsArea, OpsCode, OpsSeverity
from daimon.core.stores.domain import (
    OpsEventRow,
    OpsIssueGroupRow,
    OpsIssueStateName,
    OpsIssueStateRow,
)
from sqlalchemy import DateTime, and_, delete, func, literal, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

#: The most issues one read groups; a workspace has far fewer open at once.
MAX_ISSUES = 1000


@dataclass(frozen=True)
class OpsEventRecord:
    """An event to store, built by the emitter: what `ops_events` holds but the tenant."""

    occurred_at: datetime
    count: int
    code: OpsCode
    area: OpsArea
    severity: OpsSeverity
    reason: str | None
    fingerprint: str
    agent_id: str | None = None
    routine_id: uuid.UUID | None = None
    watch_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    platform: str | None = None
    channel_id: str | None = None
    thread_id: str | None = None
    session_id: str | None = None
    subject_key: str | None = None
    release: str | None = None
    note: str | None = None
    workflow_id: uuid.UUID | None = None
    workflow_run_id: uuid.UUID | None = None


async def record_ops_events(
    session: AsyncSession, *, tenant_id: uuid.UUID, events: Sequence[OpsEventRecord]
) -> None:
    """Append `events` to one workspace; the caller commits."""
    for event in events:
        session.add(OpsEvent(tenant_id=tenant_id, **event.__dict__))
    await session.flush()


async def list_issue_groups(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
    area: OpsArea | None = None,
    severity: OpsSeverity | None = None,
    agent_id: str | None = None,
    fingerprint: str | None = None,
    limit: int = MAX_ISSUES,
) -> list[OpsIssueGroupRow]:
    """The issues with an event since `since`, newest first, with their state, if any."""
    filters = [OpsEvent.tenant_id == tenant_id, OpsEvent.occurred_at >= since]
    if area is not None:
        filters.append(OpsEvent.area == area)
    if severity is not None:
        filters.append(OpsEvent.severity == severity)
    if agent_id is not None:
        filters.append(OpsEvent.agent_id == agent_id)
    if fingerprint is not None:
        filters.append(OpsEvent.fingerprint == fingerprint)
    last_seen = func.max(OpsEvent.occurred_at)
    grouped = await session.execute(
        select(
            OpsEvent.fingerprint,
            func.sum(OpsEvent.count),
            func.count(OpsEvent.account_id.distinct()),
            last_seen,
        )
        .where(*filters)
        .group_by(OpsEvent.fingerprint)
        .order_by(last_seen.desc(), OpsEvent.fingerprint.desc())
        .limit(limit)
    )
    found = grouped.all()
    if not found:
        return []
    fingerprints = [row[0] for row in found]
    # First seen is over all time, read only for the issues found.
    first_seen: dict[str, datetime] = {
        fp: first
        for fp, first in await session.execute(
            select(OpsEvent.fingerprint, func.min(OpsEvent.occurred_at))
            .where(OpsEvent.tenant_id == tenant_id, OpsEvent.fingerprint.in_(fingerprints))
            .group_by(OpsEvent.fingerprint)
        )
    }
    newest = await session.scalars(
        select(OpsEvent)
        .where(OpsEvent.tenant_id == tenant_id, OpsEvent.fingerprint.in_(fingerprints))
        .distinct(OpsEvent.fingerprint)
        .order_by(OpsEvent.fingerprint, OpsEvent.occurred_at.desc(), OpsEvent.id.desc())
    )
    latest = {row.fingerprint: OpsEventRow.model_validate(row) for row in newest}
    states = {
        row.fingerprint: OpsIssueStateRow.model_validate(row)
        for row in await session.scalars(
            select(OpsIssueState).where(
                OpsIssueState.tenant_id == tenant_id, OpsIssueState.fingerprint.in_(fingerprints)
            )
        )
    }
    return [
        OpsIssueGroupRow(
            fingerprint=fp,
            events=int(events),
            people=int(people),
            first_seen=first_seen[fp],
            last_seen=last,
            latest=latest[fp],
            state=states.get(fp),
        )
        for fp, events, people, last in found
    ]


async def issue_buckets(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    fingerprints: Sequence[str],
    now: datetime,
    span: timedelta,
    buckets: int,
) -> dict[str, list[int]]:
    """Event counts per `span` for each fingerprint, `buckets` of them oldest first, the
    last ending at `now`; a fingerprint with no event in the range reads all zeros."""
    out = {fp: [0] * buckets for fp in fingerprints}
    if not fingerprints:
        return out
    age = func.greatest(
        0,
        func.floor(
            func.extract("epoch", literal(now, DateTime(timezone=True)) - OpsEvent.occurred_at)
            / span.total_seconds()
        ),
    )
    rows = await session.execute(
        select(OpsEvent.fingerprint, age.label("age"), func.sum(OpsEvent.count))
        .where(
            OpsEvent.tenant_id == tenant_id,
            OpsEvent.fingerprint.in_(list(fingerprints)),
            OpsEvent.occurred_at >= now - span * buckets,
        )
        .group_by(OpsEvent.fingerprint, age)
    )
    for fingerprint, steps, total in rows:
        position = buckets - 1 - int(steps)
        if 0 <= position < buckets:
            out[fingerprint][position] += int(total)
    return out


async def list_issue_events(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    fingerprint: str,
    before: Cursor | None,
    limit: int,
) -> list[OpsEventRow]:
    """One issue's events, newest first, after the cursor `(occurred_at, id)`."""
    query = (
        select(OpsEvent)
        .where(OpsEvent.tenant_id == tenant_id, OpsEvent.fingerprint == fingerprint)
        .order_by(OpsEvent.occurred_at.desc(), OpsEvent.id.desc())
        .limit(limit)
    )
    if before is not None:
        query = query.where(tuple_(OpsEvent.occurred_at, OpsEvent.id) < before)
    return [OpsEventRow.model_validate(row) for row in await session.scalars(query)]


async def count_by_area(
    session: AsyncSession, *, tenant_id: uuid.UUID, since: datetime
) -> dict[OpsArea, int]:
    """Events since `since` per area, every state alike (charts count what is muted)."""
    rows = await session.execute(
        select(OpsEvent.area, func.sum(OpsEvent.count))
        .where(OpsEvent.tenant_id == tenant_id, OpsEvent.occurred_at >= since)
        .group_by(OpsEvent.area)
    )
    return {area: int(total) for area, total in rows}  # pyright: ignore[reportReturnType]


async def set_issue_state(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    fingerprint: str,
    state: OpsIssueStateName,
    by_account_id: uuid.UUID | None,
    at: datetime,
) -> None:
    """Resolve or mute an issue, replacing an earlier state; the caller commits."""
    row = insert(OpsIssueState).values(
        tenant_id=tenant_id,
        fingerprint=fingerprint,
        state=state,
        by_account_id=by_account_id,
        at=at,
    )
    await session.execute(
        row.on_conflict_do_update(
            index_elements=[OpsIssueState.tenant_id, OpsIssueState.fingerprint],
            set_={
                "state": row.excluded.state,
                "by_account_id": row.excluded.by_account_id,
                "at": row.excluded.at,
            },
        )
    )


async def clear_issue_state(
    session: AsyncSession, *, tenant_id: uuid.UUID, fingerprint: str
) -> None:
    """Reopen an issue; the caller commits."""
    await session.execute(
        delete(OpsIssueState).where(
            OpsIssueState.tenant_id == tenant_id, OpsIssueState.fingerprint == fingerprint
        )
    )


async def count_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    stmt = select(func.count()).where(OpsEvent.account_id == account_id)
    return int(await session.scalar(stmt) or 0)


async def delete_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Delete every event naming the person's account, in every workspace; the count."""
    result = await session.execute(
        delete(OpsEvent).where(OpsEvent.account_id == account_id).returning(OpsEvent.id)
    )
    return len(result.all())


async def prune_ops_events(session: AsyncSession, *, cutoff: datetime, batch: int) -> int:
    """Delete up to `batch` events older than `cutoff`, oldest first; the caller commits.
    Rows another transaction holds are skipped for the next run, not waited on."""
    if batch <= 0:
        raise ValueError("batch must be positive")
    victims = (
        select(OpsEvent.id)
        .where(OpsEvent.occurred_at < cutoff)
        .order_by(OpsEvent.occurred_at)
        .limit(batch)
        .with_for_update(skip_locked=True)
        .cte("victims")
        .prefix_with("MATERIALIZED")
    )
    result = await session.execute(
        delete(OpsEvent).where(OpsEvent.id.in_(select(victims.c.id))).returning(OpsEvent.id)
    )
    return len(result.all())


async def prune_issue_states(session: AsyncSession, *, cutoff: datetime) -> int:
    """Delete states set before `cutoff` for issues with no event left. A state of an issue
    still seen stays: dropping a mute would silently reopen it."""
    seen = (
        select(OpsEvent.id)
        .where(
            OpsEvent.tenant_id == OpsIssueState.tenant_id,
            OpsEvent.fingerprint == OpsIssueState.fingerprint,
        )
        .exists()
    )
    result = await session.execute(
        delete(OpsIssueState)
        .where(OpsIssueState.at < cutoff, ~seen)
        .returning(OpsIssueState.fingerprint)
    )
    return len(result.all())


async def list_issue_account_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, fingerprint: str, limit: int = 200
) -> list[uuid.UUID]:
    """The accounts with an event of one issue, most recent first."""
    last = func.max(OpsEvent.occurred_at)
    rows = await session.execute(
        select(OpsEvent.account_id)
        .where(
            OpsEvent.tenant_id == tenant_id,
            OpsEvent.fingerprint == fingerprint,
            OpsEvent.account_id.is_not(None),
        )
        .group_by(OpsEvent.account_id)
        .order_by(last.desc())
        .limit(limit)
    )
    return [account_id for (account_id,) in rows if account_id is not None]


class SubjectNames(NamedTuple):
    """What the workspace calls the routines, watches and agents some issues are about."""

    routines: dict[uuid.UUID, str | None]
    """A routine's own title, None while it has none."""
    watches: dict[uuid.UUID, str]
    """A watch has no title: its agent's name."""
    agents: dict[str, str]


async def subject_names(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    routine_ids: set[uuid.UUID],
    watch_ids: set[uuid.UUID],
) -> SubjectNames:
    """Names for the given routines and watches; a deleted one has no entry. Agent names come
    from the routines and watches seen, so an agent with neither is left to the caller."""
    routines = {
        row.id: row
        for row in (
            await session.execute(
                select(Routine.id, Routine.name, Routine.agent_id, Routine.agent_name).where(
                    Routine.tenant_id == tenant_id, Routine.id.in_(list(routine_ids))
                )
            )
        )
    }
    watches = {
        row.id: row
        for row in (
            await session.execute(
                select(Watch.id, Watch.agent_id, Watch.agent_name).where(
                    Watch.tenant_id == tenant_id, Watch.id.in_(list(watch_ids))
                )
            )
        )
    }
    agents = {row.agent_id: row.agent_name for row in (*routines.values(), *watches.values())}
    return SubjectNames(
        routines={key: row.name for key, row in routines.items()},
        watches={key: row.agent_name for key, row in watches.items()},
        agents=agents,
    )


@dataclass(frozen=True)
class WorkspaceCounts:
    routines_total: int
    routines_on: int
    routines_failing: int
    """Routines on whose last run failed."""
    watches_total: int
    watches_on: int
    watches_failing: int
    """Watches on that have failed their last checks."""


async def workspace_counts(session: AsyncSession, *, tenant_id: uuid.UUID) -> WorkspaceCounts:
    """How many routines and watches the workspace has, on, and failing."""
    routines = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Routine.enabled),
                func.count().filter(and_(Routine.enabled, Routine.last_error.is_not(None))),
            ).where(Routine.tenant_id == tenant_id)
        )
    ).one()
    live = Watch.status.in_(("active", "paused"))
    watches = (
        await session.execute(
            select(
                func.count().filter(live),
                func.count().filter(Watch.status == "active"),
                func.count().filter(and_(Watch.status == "active", Watch.consecutive_errors > 0)),
            ).where(Watch.tenant_id == tenant_id)
        )
    ).one()
    return WorkspaceCounts(*(int(x) for x in (*routines, *watches)))
