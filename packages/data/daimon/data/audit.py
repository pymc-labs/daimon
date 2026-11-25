"""The store's audit trail (`<data>.audit`): written by the store, read into the workspace's
merged trail as the `data` source."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from daimon.core.config import load_data_settings
from daimon.core.ports.audit import AuditEntry, AuditFilter, AuditSourceSpec, SourceAuditRow
from daimon.data.schema import Schemas
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def record_audit(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    account_id: uuid.UUID | None = None,
    target: str | None = None,
    detail: dict[str, object] | None = None,
    source_id: uuid.UUID | None = None,
) -> None:
    await conn.execute(
        f"insert into {schemas.data}.audit (tenant_id, account_id, kind, target, detail, source_id)"
        " values (%s, %s, %s, %s, %s, %s)".encode(),
        (tenant_id, account_id, kind, target, Jsonb(detail or {}), source_id),
    )


async def _table(session: AsyncSession) -> str | None:
    """The trail's table, or None where the store was never set up."""
    name = f"{Schemas.of(load_data_settings()).data}.audit"
    found = await session.scalar(text("select to_regclass(:t) is not null"), {"t": name})
    return name if found else None


async def _read(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: AuditFilter,
    before: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[AuditEntry]:
    table = await _table(session)
    if table is None:
        return []
    rows = await session.execute(
        text(
            f"select id, at, account_id, kind, target, detail from {table}"
            " where tenant_id = :tenant"
            " and (cast(:before_at as timestamptz) is null or (at, id) < (:before_at, :before_id))"
            " and (cast(:actor as uuid) is null or account_id = :actor)"
            " and (cast(:kind as text) is null or kind = :kind)"
            " and (cast(:since as timestamptz) is null or at >= :since)"
            " and (cast(:until as timestamptz) is null or at < :until)"
            " order by at desc, id desc limit :limit"
        ),
        {
            "tenant": tenant_id,
            "before_at": before[0] if before else None,
            "before_id": before[1] if before else None,
            "actor": query.actor_account_id,
            "kind": query.kind,
            "since": query.since,
            "until": query.until,
            "limit": limit,
        },
    )
    return [
        AuditEntry(
            "data",
            r.id,
            r.at,
            r.account_id,
            r.kind,
            SourceAuditRow(
                r.id, r.at, r.kind, r.target, json.dumps(r.detail) if r.detail else None
            ),
        )
        for r in rows
    ]


async def _kinds(session: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    table = await _table(session)
    if table is None:
        return []
    rows = await session.scalars(
        text(f"select distinct kind from {table} where tenant_id = :tenant order by kind"),
        {"tenant": tenant_id},
    )
    return list(rows)


AUDIT_SOURCE = AuditSourceSpec(read=_read, kinds=_kinds)
