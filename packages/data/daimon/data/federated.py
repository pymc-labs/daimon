"""Live sources: asked upstream at read time instead of stored.

A live source (`mode` federated, its kind's connector sets `live` and implements
`live_search`) is searched with the caller's query when a search runs. It is scope-gated: it
answers only a caller whose context holds its default scope, never an access-list reader.
It is read-only and its results are never stored. Each call is metered like a search arm
through the caller's `meter`. It fails open: a source that errors or is still running when the
search's time budget ends is left out, and the rest answer.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

import structlog
from daimon.data import audit
from daimon.data.access import ReadContext
from daimon.data.schema import Schemas
from daimon.data.sync.connector import CONNECTORS, LiveHit
from daimon.data.sync.engine import Env, build_source
from psycopg import AsyncConnection
from psycopg.rows import DictRow

log = structlog.get_logger()

type Meter = Callable[[uuid.UUID, int], Awaitable[None]]
"""Called once per live call with the source and how many hits it returned."""

LIMIT: Final = 10
"""Hits one source returns to one search, at most."""


def audit_meter(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
) -> Meter:
    """A meter that records each live call in the store's audit trail as the asker's."""

    async def meter(source_id: uuid.UUID, hits: int) -> None:
        await audit.record_audit(
            conn, schemas, tenant_id=tenant_id, kind="source.live_search",
            account_id=account_id, source_id=source_id, detail={"hits": hits},
        )  # fmt: skip

    return meter


@dataclass(frozen=True)
class LiveResult:
    source_id: uuid.UUID
    source_name: str
    hits: tuple[LiveHit, ...]
    skipped: str | None = None
    """`timeout` or `error` when the source gave no answer; hits are then empty."""


async def search(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    env: Env,
    ctx: ReadContext,
    query: str,
    *,
    seconds: float,
    limit: int = LIMIT,
    meter: Meter | None = None,
) -> list[LiveResult]:
    """One result per live source the caller may read, within `seconds` for all of them."""
    d = schemas.data
    cur = await conn.execute(
        f"select s.id, s.tenant_id, s.kind, s.name, s.provider, s.config, s.credential,"
        f" s.default_scope, null::jsonb as state, null::timestamptz as last_sync_at"
        f" from {d}.sources s where s.mode = 'federated' and s.publication = 'published'"
        " and s.status = 'active' and s.tenant_id = any(%s) and s.default_scope = any(%s)"
        " order by s.name, s.id".encode(),
        (list(ctx.tenant_ids), [x for x in ctx.scopes if x != "restricted:acl"]),
    )
    rows = [
        r for r in await cur.fetchall() if CONNECTORS.get(r["kind"]) and CONNECTORS[r["kind"]].live
    ]

    async def one(row: DictRow) -> LiveResult:
        source = build_source(row, env, None)
        try:
            hits = await CONNECTORS[source.kind].live_search(source, query, limit)
            if meter:
                await meter(source.id, len(hits))
        except Exception as e:
            log.warning("data.live_failed", kind=source.kind, error=type(e).__name__)
            return LiveResult(source.id, source.name, (), "error")
        return LiveResult(source.id, source.name, tuple(hits[:limit]))

    if not rows:
        return []
    tasks = {asyncio.create_task(one(r)): r for r in rows}
    done, late = await asyncio.wait(tasks, timeout=seconds)
    for task in late:
        task.cancel()
    await asyncio.gather(*late, return_exceptions=True)
    return [
        task.result() if task in done else LiveResult(r["id"], r["name"], (), "timeout")
        for task, r in tasks.items()
    ]
