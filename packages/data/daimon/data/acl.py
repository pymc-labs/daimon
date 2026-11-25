"""Access lists: the writers a sync calls, and the nightly check.

A record carries one access list (`acl`). Who holds a list is materialized by `publish` (a
unit's whole permission pass), `intern` (a list's definition) and the delete triggers;
`set_records` gives records their lists. All of it is SQL in `0001_store.sql`; these are thin
wrappers. Each takes a connection of the store's owner inside the caller's transaction.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Final, NotRequired, TypedDict
from uuid import UUID

import psycopg
from daimon.data.schema import Schemas
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

RETRIED: Final = (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure)


class Ref(TypedDict):
    """One entry of a leaf: a subject, or a group whose members a unit publishes."""

    ref: str
    group: bool
    unit: int | None


class RecordAccess(TypedDict):
    """A record's list as read: `ext` its `<kind>:<id>` key; `held` hides it until a later read."""

    ext: str
    acl: int
    until: NotRequired[datetime | None]
    held: NotRequired[bool]
    why: NotRequired[str | None]


def ref(subject: str, *, group: bool = False, unit_id: int | None = None) -> Ref:
    return {"ref": subject, "group": group, "unit": unit_id}


async def retry[T](
    work: Callable[[], Awaitable[T]], rollback: Callable[[], Awaitable[None]], attempts: int = 4
) -> T:
    """Run one evidence-writing transaction again after a deadlock or serialization failure."""
    for n in range(attempts):
        try:
            return await work()
        except RETRIED:
            await rollback()
            if n == attempts - 1:
                raise
            await asyncio.sleep(random.uniform(0.02, 0.08) * (n + 1))
    raise AssertionError("unreachable")


async def _one(conn: AsyncConnection[DictRow], sql: str, params: Sequence[object]) -> object:
    """The first column of the first row; None for no row."""
    row = await (await conn.execute(sql.encode(), params)).fetchone()
    return next(iter(row.values())) if row else None


async def _int(conn: AsyncConnection[DictRow], sql: str, params: Sequence[object]) -> int | None:
    found = await _one(conn, sql, params)
    return found if isinstance(found, int) else None


async def lock(conn: AsyncConnection[DictRow], schemas: Schemas) -> None:
    """The membership lock: the first lock of a transaction that writes evidence."""
    await conn.execute(f"select {schemas.data}.membership_lock()".encode())


async def unit(
    conn: AsyncConnection[DictRow], schemas: Schemas, source_id: UUID, kind: str, ref_: str
) -> int:
    """The evidence unit of a pass, a connection or a group read: created expired, live once
    `publish` renews it. An existing unit is read, never updated: an update would hold its row
    lock to the end and could deadlock with a publish of it."""
    find = (
        f"select id from {schemas.data}.evidence_units"
        " where source_id = %s and kind = %s and ref = %s"
    )
    for _ in range(2):
        found = await _int(conn, find, (source_id, kind, ref_))
        if found is not None:
            return found
        await lock(conn, schemas)
        made = await _int(
            conn,
            f"insert into {schemas.data}.evidence_units (source_id, kind, ref) values (%s, %s, %s)"
            " on conflict do nothing returning id",
            (source_id, kind, ref_),
        )
        if made is not None:
            return made
    raise LookupError(f"evidence unit {kind}:{ref_} vanished while it was made")


async def publish(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    unit_id: int,
    seq: int,
    groups: dict[str, list[str]] | None = None,
    fetched_at: datetime | None = None,
    lifetime: timedelta = timedelta(hours=1),
) -> bool:
    """A unit's whole pass; `groups` is its complete batch. False: an older pass, discarded."""
    ok = await _one(
        conn,
        f"select {schemas.data}.publish(%s, %s, %s, coalesce(%s, now()), %s)",
        (unit_id, seq, Jsonb(groups or {}), fetched_at, lifetime),
    )
    return ok is True


async def intern(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    source_id: UUID,
    refs: Sequence[Ref] = (),
    *,
    all_of: Sequence[int] | None = None,
    personal: bool = False,
) -> int:
    """A leaf from refs, or a conjunction of the source's leaves; one definition is one id."""
    found = await _int(
        conn,
        f"select {schemas.data}.intern_acl(%s, %s, %s, %s)",
        (source_id, Jsonb(list(refs)), list(all_of) if all_of is not None else None, personal),
    )
    if found is None:
        raise LookupError("intern_acl returned no id")
    return found


async def lookup(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    source_id: UUID,
    refs: Sequence[Ref] = (),
    *,
    all_of: Sequence[int] | None = None,
    personal: bool = False,
) -> int | None:
    """The id `intern` would return, or None; writes nothing and takes no lock."""
    return await _int(
        conn,
        f"select {schemas.data}.acl_lookup(%s, %s, %s, %s)",
        (source_id, Jsonb(list(refs)), list(all_of) if all_of is not None else None, personal),
    )


async def set_records(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    source_id: UUID,
    records: Sequence[RecordAccess],
    read_at: datetime,
    copies: Sequence[str] = (),
) -> int:
    """Each record's list on the record and on every table in `copies`; held ones read as 0."""
    done = await _int(
        conn,
        f"select {schemas.data}.set_record_acls(%s, %s::regclass[], %s, %s)",
        (source_id, list(copies), Jsonb(list(records), dumps=_dumps), read_at),
    )
    return done or 0


def _dumps(value: object) -> str:
    return json.dumps(value, default=str)


async def copies_differ(conn: AsyncConnection[DictRow], schemas: Schemas) -> dict[str, int]:
    """Per copy table (records, and any table with source_id, external_id and acl): copies
    whose list is not their record's, or that carry a list with no record access row. A
    held record's copy may be at 0 or at its own list."""
    tables = await conn.execute(
        """select c.relname as name from pg_class c
        where c.relnamespace = %s::regnamespace and c.relkind = 'r'
          and (select count(*) from pg_attribute a where a.attrelid = c.oid and a.attnum > 0
               and not a.attisdropped and a.attname in ('source_id', 'external_id', 'acl')) = 3
        order by 1""",
        (schemas.data,),
    )
    out: dict[str, int] = {}
    for row in await tables.fetchall():
        name = str(row["name"])
        wrong = await _int(
            conn,
            f"""select count(*) from {schemas.data}.{name} x
            left join {schemas.data}.record_access r
              on r.source_id = x.source_id and r.external_id = x.external_id
            where (r.source_id is null and x.acl is not null)
               or (r.source_id is not null and x.acl is distinct from r.acl_id
                   and not (r.held_at is not null and x.acl = 0))""",
            (),
        )
        out[name] = wrong or 0
    return out


async def nightly(
    conn: AsyncConnection[DictRow], schemas: Schemas, *, repair: bool = True
) -> dict[str, int]:
    """Rebuild grants and holder sets from definitions, comparing both ways; count copies that
    disagree with their record; refresh each list's row count. Any non-zero count is an alert."""
    cur = await conn.execute(
        f"select what, n from {schemas.data}.acl_rebuild(%s)".encode(), (repair,)
    )
    out = {str(r["what"]): int(r["n"]) for r in await cur.fetchall()}
    out.update({f"copies:{t}": n for t, n in (await copies_differ(conn, schemas)).items()})
    await conn.execute(
        f"""update {schemas.data}.acls a set rows = n.n
        from (select l.id, (select count(*) from {schemas.data}.records r where r.acl = l.id) as n
              from {schemas.data}.acls l) n
        where n.id = a.id and a.rows <> n.n""".encode()
    )
    for table in ("acls", "acl_conjunctions", "evidence_units", "holder_sets"):
        await conn.execute(f"analyze {schemas.data}.{table}".encode())
    return out
