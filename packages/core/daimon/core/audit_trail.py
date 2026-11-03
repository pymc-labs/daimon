"""One newest-first trail of what happened in a workspace, read from several sources.

Each source keeps its own table and its own fields; this module only merges them.
`web`: what admins changed in the web app. `security`: chat tool calls and panel
writes. A new source is one `AuditSourceSpec` in `_SOURCES` plus its name in
`AuditSource`; filters, paging and the merge need no other change.

Every source pages by `(time, id)` keyset, so a merged page is the newest rows of each
source cut at the page size, and a cursor from one page works for all sources.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, Literal

from daimon.core.cursor import Cursor
from daimon.core.stores import security_audit, web_audit
from daimon.core.stores.domain import WebAuditRow
from daimon.core.stores.security_audit import SecurityAuditRow
from sqlalchemy.ext.asyncio import AsyncSession

type AuditSource = Literal["web", "security"]


@dataclass(frozen=True)
class AuditFilter:
    """Which rows to read. `since` is inclusive, `until` exclusive."""

    source: AuditSource | None = None
    actor_account_id: uuid.UUID | None = None
    kind: str | None = None
    since: datetime | None = None
    until: datetime | None = None


@dataclass(frozen=True)
class AuditEntry:
    """A row of one source, with what every source shares."""

    source: AuditSource
    id: uuid.UUID
    at: datetime
    actor_account_id: uuid.UUID | None
    kind: str
    """`web`: the action. `security`: the tool."""
    row: WebAuditRow | SecurityAuditRow

    @property
    def cursor(self) -> Cursor:
        return (self.at, self.id)


@dataclass(frozen=True)
class AuditKind:
    source: AuditSource
    kind: str


type _Read = Callable[
    [AsyncSession, uuid.UUID, AuditFilter, Cursor | None, int], Awaitable[list[AuditEntry]]
]
type _Kinds = Callable[[AsyncSession, uuid.UUID], Awaitable[list[str]]]


@dataclass(frozen=True)
class AuditSourceSpec:
    read: _Read
    """Rows matching the filter's actor, kind and dates, newest first, after the cursor."""
    kinds: _Kinds


async def _read_web(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: AuditFilter,
    before: Cursor | None,
    limit: int,
) -> list[AuditEntry]:
    rows = await web_audit.list_recent(
        session,
        tenant_id=tenant_id,
        limit=limit,
        before=before,
        actor_account_id=query.actor_account_id,
        action=query.kind,
        since=query.since,
        until=query.until,
    )
    return [AuditEntry("web", r.id, r.at, r.actor_account_id, r.action, r) for r in rows]


async def _read_security(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: AuditFilter,
    before: Cursor | None,
    limit: int,
) -> list[AuditEntry]:
    rows = await security_audit.list_recent(
        session,
        tenant_id=tenant_id,
        limit=limit,
        before=before,
        account_id=query.actor_account_id,
        tool_name=query.kind,
        since=query.since,
        until=query.until,
    )
    return [AuditEntry("security", r.id, r.occurred_at, r.account_id, r.tool_name, r) for r in rows]


_SOURCES: Final[dict[AuditSource, AuditSourceSpec]] = {
    "web": AuditSourceSpec(
        read=_read_web,
        kinds=lambda session, tenant_id: web_audit.list_actions(session, tenant_id=tenant_id),
    ),
    "security": AuditSourceSpec(
        read=_read_security,
        kinds=lambda session, tenant_id: security_audit.list_tool_names(
            session, tenant_id=tenant_id
        ),
    ),
}


def _chosen(query: AuditFilter) -> list[AuditSource]:
    return [query.source] if query.source is not None else list(_SOURCES)


def day_bounds(first: date | None, last: date | None) -> tuple[datetime | None, datetime | None]:
    """`since` and `until` covering the UTC days `first` to `last`, both included."""
    since = datetime.combine(first, time.min, tzinfo=UTC) if first else None
    until = datetime.combine(last + timedelta(days=1), time.min, tzinfo=UTC) if last else None
    return since, until


async def list_entries(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query: AuditFilter | None = None,
    before: Cursor | None = None,
    limit: int = 100,
) -> list[AuditEntry]:
    """The newest `limit` rows of the chosen sources that match, newest first."""
    query = query or AuditFilter()
    found: list[AuditEntry] = []
    for name in _chosen(query):
        found.extend(await _SOURCES[name].read(session, tenant_id, query, before, limit))
    found.sort(key=lambda e: e.cursor, reverse=True)
    return found[:limit]


async def list_kinds(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[AuditKind]:
    """Every kind each source has recorded, for a filter's options."""
    return [
        AuditKind(name, kind)
        for name, spec in _SOURCES.items()
        for kind in await spec.kinds(session, tenant_id)
    ]
