"""Browsing and reading what a source holds: its list, records, files and datasets.

Each function takes the store owner's connection and a `ReadContext`, and reads inside
`access.scoped`, so it returns only what that reader may see.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from daimon.data import access, recheck, redaction
from daimon.data.access import ReadContext
from daimon.data.schema import Schemas
from daimon.data.sync.connector import CONNECTORS
from daimon.data.sync.engine import Env, build_source
from psycopg import AsyncConnection
from psycopg.rows import DictRow

PAGE = 50


@dataclass(frozen=True)
class SourceInfo:
    id: UUID
    name: str
    kind: str
    status: str
    records: int
    """How many of its records this reader may see."""


@dataclass(frozen=True)
class Item:
    id: UUID
    source_id: UUID
    external_id: str
    kind: str
    title: str
    url: str | None
    author: str | None
    occurred_at: datetime
    scope: str


@dataclass(frozen=True)
class Attachment:
    ord: int
    name: str
    content_type: str
    size: int


@dataclass(frozen=True)
class Record:
    item: Item
    body: str
    payload: dict[str, Any]
    attachments: list[Attachment]


@dataclass(frozen=True)
class Page:
    items: list[Item]
    next: str | None
    """Pass as `after` for the following page; None at the end."""


@dataclass(frozen=True)
class Dataset:
    source_id: UUID
    name: str
    title: str
    description: str
    columns: list[dict[str, Any]]
    metrics: list[dict[str, str]]


_ITEM = "r.id, r.source_id, r.external_id, r.kind, r.title, r.url, r.author, r.occurred_at, r.scope"


def _item(r: DictRow) -> Item:
    return Item(
        r["id"], r["source_id"], r["external_id"], r["kind"], r["title"], r["url"], r["author"],
        r["occurred_at"], r["scope"],
    )  # fmt: skip


def _cursor(item: Item) -> str:
    return base64.urlsafe_b64encode(f"{item.occurred_at.isoformat()}|{item.id}".encode()).decode()


def _position(after: str) -> tuple[datetime, UUID]:
    try:
        when, rid = base64.urlsafe_b64decode(after.encode()).decode().split("|")
        return datetime.fromisoformat(when), UUID(rid)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ValueError("not a cursor from this list") from None


async def sources(
    conn: AsyncConnection[DictRow], schemas: Schemas, ctx: ReadContext
) -> list[SourceInfo]:
    """The sources this reader sees, with how many records of each they may read."""
    d = schemas.data
    async with access.scoped(conn, schemas, ctx):
        cur = await conn.execute(
            f"select s.id, s.name, s.kind, s.status, (select count(*) from {d}.records r"
            f" where r.source_id = s.id) as records from {d}.sources s"
            " order by s.name, s.id".encode()
        )
        return [
            SourceInfo(r["id"], r["name"], r["kind"], r["status"], r["records"])
            for r in await cur.fetchall()
        ]


async def freshness(
    conn: AsyncConnection[DictRow], schemas: Schemas, ctx: ReadContext
) -> dict[UUID, datetime]:
    """When the newest record this reader may see of each source happened."""
    d = schemas.data
    async with access.scoped(conn, schemas, ctx):
        cur = await conn.execute(
            f"select source_id, max(occurred_at) as latest from {d}.records"
            " group by source_id".encode()
        )
        return {r["source_id"]: r["latest"] for r in await cur.fetchall()}


async def browse(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    ctx: ReadContext,
    source_id: UUID,
    *,
    kind: str | None = None,
    after: str | None = None,
    limit: int = PAGE,
) -> Page:
    """A source's records, newest first, a page at a time."""
    d = schemas.data
    args: dict[str, Any] = {"source": source_id, "kind": kind, "n": limit + 1}
    seek = ""
    if after is not None:
        args["at"], args["id"] = _position(after)
        seek = " and (r.occurred_at, r.id) < (%(at)s, %(id)s)"
    async with access.scoped(conn, schemas, ctx):
        cur = await conn.execute(
            f"select {_ITEM} from {d}.records r where r.source_id = %(source)s"
            f" and (%(kind)s::text is null or r.kind = %(kind)s){seek}"
            " order by r.occurred_at desc, r.id desc limit %(n)s".encode(),
            args,
        )
        rows = await cur.fetchall()
    items = [_item(r) for r in rows[:limit]]
    return Page(items, _cursor(items[-1]) if len(rows) > limit else None)


async def get(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    ctx: ReadContext,
    record_id: UUID,
    env: Env | None = None,
) -> Record | None:
    """One record with its text and attached files; None when it does not exist or this
    reader may not see it (the two look alike). With `env`, a source that re-checks reads is
    asked again (`recheck`)."""
    d = schemas.data
    async with access.scoped(conn, schemas, ctx):
        cur = await conn.execute(
            f"select {_ITEM}, r.body, r.payload from {d}.records r where r.id = %s".encode(),
            (record_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        cur = await conn.execute(
            f"select ord, name, content_type, size from {d}.attachments"
            " where source_id = %s and external_id = %s order by ord".encode(),
            (row["source_id"], row["external_id"]),
        )
        files = [
            Attachment(a["ord"], a["name"], a["content_type"], a["size"])
            for a in await cur.fetchall()
        ]
    ref = (row["source_id"], row["external_id"])
    if env is not None and ref not in await recheck.allowed(conn, schemas, env, ctx.readers, [ref]):
        return None
    return Record(_item(row), row["body"], row["payload"], files)


async def read_file(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    ctx: ReadContext,
    env: Env,
    record_id: UUID,
    index: int,
) -> tuple[bytes, str] | None:
    """The bytes and content type of a record's attached file, asked of its source now with
    the source's own credential (the store keeps no bytes). None when the reader may not see
    the record, it has no such file, or the source no longer shows it."""
    record = await get(conn, schemas, ctx, record_id, env)
    file = next((a for a in record.attachments if a.ord == index), None) if record else None
    if record is None or file is None:
        return None
    d = schemas.data
    cur = await conn.execute(
        f"select s.id, s.tenant_id, s.kind, s.name, s.provider, s.config, s.credential,"
        f" s.default_scope, s.redaction_set, c.state, c.last_sync_at from {d}.sources s"
        f" left join {d}.source_cursors c on c.source_id = s.id where s.id = %s".encode(),
        (record.item.source_id,),
    )
    row = await cur.fetchone()
    connector = CONNECTORS.get(row["kind"]) if row else None
    if row is None or connector is None:
        return None
    cur = await conn.execute(
        f"select payload from {d}.records where id = %s".encode(), (record_id,)
    )
    payload = (await cur.fetchone() or {"payload": {}})["payload"]
    try:
        redactor = (
            await redaction.ingest_redactor(conn, schemas, row["tenant_id"], row["redaction_set"])
            if row["redaction_set"]
            else None
        )
    except redaction.NotApproved:
        return None
    source = build_source(row, env, None)
    try:
        got = await connector.fetch_file(
            source, payload, record.item.external_id, file.name, file.ord
        )
        if got is None or redactor is None or not got[1].startswith("text/"):
            return got
        return redactor.text(got[0].decode("utf-8", "replace")).encode(), got[1]
    finally:
        await connector.release(source)


async def datasets(
    conn: AsyncConnection[DictRow], schemas: Schemas, ctx: ReadContext
) -> list[Dataset]:
    """The datasets caller SQL may query by name, with their columns and metrics."""
    d = schemas.data
    async with access.scoped(conn, schemas, ctx):
        cur = await conn.execute(
            f"select source_id, name, title, description, columns from {d}.datasets"
            " order by source_id, name".encode()
        )
        sets = await cur.fetchall()
        cur = await conn.execute(
            f"select source_id, dataset, name, description, expr from {d}.metrics"
            " order by name".encode()
        )
        metrics = await cur.fetchall()
    return [
        Dataset(
            s["source_id"], s["name"], s["title"], s["description"], s["columns"],
            [
                {"name": m["name"], "description": m["description"], "expr": m["expr"]}
                for m in metrics
                if m["source_id"] == s["source_id"] and m["dataset"] == s["name"]
            ],
        )
        for s in sets
    ]  # fmt: skip
