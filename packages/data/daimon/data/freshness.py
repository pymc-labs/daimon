"""How stale a source may get before it is hidden.

A source with a ceiling is read only while its last complete sync is younger than its
lifetime: three sync intervals, at least the floor and at most the ceiling. Past that, every
read treats it as unpublished until the next complete sync. The rule is `source_fresh` in
`0005_ops.sql`, which `visible_sources()` applies on every path; this module reads its
state for display and tests. A live source stores nothing and is never hidden.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from daimon.data.schema import Schemas
from psycopg import AsyncConnection
from psycopg.rows import DictRow


@dataclass(frozen=True)
class Freshness:
    fresh: bool
    last_sync_at: datetime | None
    lifetime: timedelta | None


async def state(
    conn: AsyncConnection[DictRow], schemas: Schemas, source_id: uuid.UUID
) -> Freshness | None:
    """The source's freshness as the database judges it now; None for an unknown source."""
    d = schemas.data
    cur = await conn.execute(
        f"select {d}.source_fresh(s.id) as fresh, c.last_sync_at,"
        f" {d}.source_lifetime(s.fresh_floor_minutes, s.fresh_ceiling_minutes, s.config) as life,"
        f" s.fresh_ceiling_minutes is not null as limited"
        f" from {d}.sources s left join {d}.source_cursors c on c.source_id = s.id"
        " where s.id = %s".encode(),
        (source_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return Freshness(
        bool(row["fresh"]), row["last_sync_at"], row["life"] if row["limited"] else None
    )
