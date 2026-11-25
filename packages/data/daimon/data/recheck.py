"""The live re-check of stored content.

A source whose connector sets `recheck_reads` and implements `can_open` is asked, for each
stored record about to be shown, whether every person the caller reads for may still open it
in the source itself. A record that needs the check and did not pass in this request shows
nothing: a refusal, an error, a timeout, a reader the source does not know, or going over
`MAX_CHECKS` all drop it. Nothing is kept between requests. Callers pass the records their
own read returned and show only what comes back.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import Final

import structlog
from daimon.data.schema import Schemas
from daimon.data.sync import request_cache
from daimon.data.sync.connector import CONNECTORS, Source
from daimon.data.sync.engine import Env, build_source, with_people
from psycopg import AsyncConnection
from psycopg.rows import DictRow

log = structlog.get_logger()

MAX_CHECKS: Final = 12
"""Records one request may check; the rest of those that need it are dropped."""
SECONDS: Final = 10.0
"""One check's limit."""

type Ref = tuple[uuid.UUID, str]
"""A stored record: its source and its `external_id`."""


async def reader_subjects(
    conn: AsyncConnection[DictRow], schemas: Schemas, source: Source, readers: Sequence[str]
) -> list[tuple[str, ...]]:
    """For each reader, their subjects in this source's namespace: a person's linked
    identities, or the subject itself. An empty tuple: the source cannot be asked about them."""
    workspace = CONNECTORS[source.kind].workspace_of(source)
    prefix = f"{source.provider}/{workspace}:user:"
    accounts = [r.removeprefix("account:") for r in readers if r.startswith("account:")]
    linked: dict[str, list[str]] = {}
    if accounts:
        cur = await conn.execute(
            f"select account_id::text as account, subject from {schemas.data}.identities"
            " where tenant_id = %s and provider = %s and workspace = %s"
            " and account_id::text = any(%s) and (active or proof = 'oauth')".encode(),
            (source.tenant_id, source.provider, workspace, accounts),
        )
        for row in await cur.fetchall():
            linked.setdefault(row["account"], []).append(row["subject"])
    return [
        tuple(linked.get(r.removeprefix("account:"), ()))
        if r.startswith("account:")
        else (r.removeprefix(prefix),)
        if r.startswith(prefix)
        else ()
        for r in readers
    ]


async def _ask(source: Source, external_id: str, subjects: Sequence[tuple[str, ...]]) -> bool:
    connector = CONNECTORS[source.kind]
    try:
        async with asyncio.timeout(SECONDS):
            answers = await asyncio.gather(
                *(connector.can_open(source, external_id, s) for s in subjects)
            )
    except Exception as e:
        log.warning("data.recheck_failed", kind=source.kind, error=type(e).__name__)
        return False
    return all(a is True for a in answers)


async def allowed(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    env: Env,
    readers: Sequence[str],
    refs: Sequence[Ref],
    limit: int = MAX_CHECKS,
) -> set[Ref]:
    """The records of `refs` that may be shown to `readers` now: those whose source needs no
    check, and those the source itself still opens to every reader."""
    if not refs:
        return set()
    d = schemas.data
    cur = await conn.execute(
        f"select s.id, s.tenant_id, s.kind, s.name, s.provider, s.config, s.credential,"
        f" s.default_scope, c.state, c.last_sync_at from {d}.sources s"
        f" left join {d}.source_cursors c on c.source_id = s.id where s.id = any(%s)".encode(),
        (list({r[0] for r in refs}),),
    )
    sources = {row["id"]: build_source(row, env, None) for row in await cur.fetchall()}
    out: set[Ref] = set()
    pending: list[Ref] = []
    for ref in refs:
        src = sources.get(ref[0])
        connector = CONNECTORS.get(src.kind) if src else None
        if src is None or connector is None:
            continue  # not stored any more: fail closed
        if connector.recheck_reads and connector.has_can_open():
            pending.append(ref)
        else:
            out.add(ref)
    asked = pending[:limit]
    for sid in {r[0] for r in asked}:
        sources[sid] = await with_people(conn, env, sources[sid])
    subjects = {
        sid: await reader_subjects(conn, schemas, sources[sid], readers)
        for sid in {r[0] for r in asked}
    }
    async with request_cache.scope():
        verdicts = await asyncio.gather(
            *(
                _ask(sources[sid], ext, subjects[sid]) if readers and all(subjects[sid]) else _no()
                for sid, ext in asked
            )
        )
    out.update(ref for ref, ok in zip(asked, verdicts, strict=True) if ok)
    return out


async def _no() -> bool:
    return False
