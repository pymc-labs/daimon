"""Live sources: asked upstream at read time instead of stored.

A live source (`mode` federated, its kind's connector sets `live` and implements
`live_search`) is searched with the caller's query when a search runs. It is scope-gated: it
answers only a caller whose context holds its default scope, never an access-list reader.
It is read-only and its results are never stored, but go through the source's redaction set
as a stored record's text does: `exclude_ids` drop a hit, and a set not approved leaves the
source out. Each call is metered like a search arm through the caller's `meter`. It fails
open: a source that errors or is still running when the search's time budget ends is left
out, and the rest answer.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Final

import structlog
from daimon.data import audit, redaction
from daimon.data.access import ReadContext
from daimon.data.recheck import reader_subjects
from daimon.data.schema import Schemas
from daimon.data.sync import request_cache
from daimon.data.sync.connector import CONNECTORS, LiveHit, Source
from daimon.data.sync.engine import Env, build_source, ready
from psycopg import AsyncConnection
from psycopg.rows import DictRow

log = structlog.get_logger()

type Meter = Callable[[uuid.UUID, int], Awaitable[None]]
"""Called once per live call with the source and how many hits it returned."""

LIMIT: Final = 10
"""Hits one source returns to one search, at most."""
READERS: Final = 4
"""Per-reader live searches one search runs at once."""


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
        f" s.default_scope, s.redaction_set, null::jsonb as state,"
        " null::timestamptz as last_sync_at"
        f" from {d}.sources s where s.mode = 'federated' and s.publication = 'published'"
        " and s.status = 'active' and s.tenant_id = any(%s) and s.default_scope = any(%s)"
        " order by s.name, s.id".encode(),
        (list(ctx.tenant_ids), [x for x in ctx.scopes if x != "restricted:acl"]),
    )
    rows = [
        r for r in await cur.fetchall() if CONNECTORS.get(r["kind"]) and CONNECTORS[r["kind"]].live
    ]

    people: dict[uuid.UUID, list[tuple[str, ...]]] = {}
    for r in rows:
        if CONNECTORS[r["kind"]].live_as_reader:
            people[r["id"]] = await reader_subjects(
                conn, schemas, build_source(r, env, None), ctx.readers
            )

    gate = asyncio.Semaphore(READERS)

    async def read(source: Source, query: str, limit: int) -> list[LiveHit]:
        connector = CONNECTORS[source.kind]
        if not connector.live_as_reader:
            return await connector.live_search(source, query, limit)
        who = people[source.id]
        if not who or not all(who):
            raise PermissionError("a reader the source cannot act as")

        async def ask(subjects: tuple[str, ...]) -> list[LiveHit]:
            async with gate:
                return await connector.live_search_as(source, query, limit, subjects)

        found = [{h.ref: h for h in x} for x in await asyncio.gather(*(ask(s) for s in who))]
        return [
            replace(h, text=h.text + h.detail if same else h.text, detail="")
            for ref, h in found[0].items()
            if all(ref in x for x in found[1:])
            for same in [all(x[ref].detail == h.detail for x in found[1:])]
        ]

    async def one(row: DictRow) -> LiveResult:
        try:
            rules = (
                await redaction.ingest_redactor(
                    conn, schemas, row["tenant_id"], row["redaction_set"]
                )
                if row["redaction_set"]
                else None
            )
        except redaction.NotApproved:
            return LiveResult(row["id"], row["name"], (), "error")
        source = await ready(conn, env, build_source(row, env, None))
        connector = CONNECTORS[source.kind]
        try:
            hits = await read(source, query, limit)
            if meter:
                await meter(source.id, len(hits))
        except Exception as e:
            log.warning("data.live_failed", kind=source.kind, error=type(e).__name__)
            return LiveResult(source.id, source.name, (), "error")
        finally:
            await connector.release(source)
        if rules is not None:
            hits = [_redacted(rules, h) for h in hits if not rules.excludes(h.ref)]
        return LiveResult(source.id, source.name, tuple(hits[:limit]))

    if not rows:
        return []
    async with request_cache.scope():
        tasks = {asyncio.create_task(one(r)): r for r in rows}
        done, late = await asyncio.wait(tasks, timeout=seconds)
        for task in late:
            task.cancel()
        await asyncio.gather(*late, return_exceptions=True)
        return [
            task.result() if task in done else LiveResult(r["id"], r["name"], (), "timeout")
            for task, r in tasks.items()
        ]


def _redacted(rules: redaction.Redactor, hit: LiveHit) -> LiveHit:
    text = rules.text
    return replace(
        hit,
        title=text(hit.title),
        text=text(hit.text),
        author=None if hit.author is None else text(hit.author),
    )
