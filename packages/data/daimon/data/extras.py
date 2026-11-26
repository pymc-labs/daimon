"""Live text added to stored records as they are read, never stored (Toggl rates and costs).

A source whose connector implements `live_extra` is asked, for the records about to be shown,
what it adds for the people the caller reads for, as their own subjects in the source. It
fails closed: a reader the source does not know, an error or a timeout adds nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import Final

import structlog
from daimon.data.recheck import Ref, reader_subjects
from daimon.data.schema import Schemas
from daimon.data.sync.connector import CONNECTORS
from daimon.data.sync.engine import Env, build_source, ready
from psycopg import AsyncConnection
from psycopg.rows import DictRow

log = structlog.get_logger()

SECONDS: Final = 5.0
"""One source's limit."""


async def added(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    env: Env,
    readers: Sequence[str],
    refs: Sequence[Ref],
    seconds: float = SECONDS,
) -> dict[Ref, str]:
    """The text each of `refs` gains for `readers` now, where its source adds any."""
    kinds = [k for k, c in CONNECTORS.items() if c.has_live_extra()]
    if not refs or not readers or not kinds:
        return {}
    cur = await conn.execute(
        f"select s.id, s.tenant_id, s.kind, s.name, s.provider, s.config, s.credential,"
        f" s.default_scope, null::jsonb as state, null::timestamptz as last_sync_at"
        f" from {schemas.data}.sources s where s.id = any(%s) and s.kind = any(%s)".encode(),
        (list({r[0] for r in refs}), kinds),
    )
    out: dict[Ref, str] = {}
    for row in await cur.fetchall():
        sid: uuid.UUID = row["id"]
        source = await ready(conn, env, build_source(row, env, None))
        connector = CONNECTORS[source.kind]
        subjects = await reader_subjects(conn, schemas, source, readers)
        if not all(subjects):
            continue
        try:
            async with asyncio.timeout(seconds):
                got = await connector.live_extra(
                    source, [ext for s, ext in refs if s == sid], subjects
                )
        except Exception as e:
            log.warning("data.live_extra_failed", kind=source.kind, error=type(e).__name__)
            continue
        finally:
            await connector.release(source)
        out.update(((sid, ext), text) for ext, text in got.items())
    return out
