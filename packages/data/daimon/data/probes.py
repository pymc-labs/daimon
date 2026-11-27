"""`daimon data probe`: read-only checks of a tenant's store, for after a deploy or a doubt.

Each probe returns counts and ids, no record text. All but `person` run in a read-only
transaction, so they cannot write. `person` reads as an account through `access.scoped`, which
records only the transient read context and audience cache every read leaves.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from daimon.data import access, identity, reads
from daimon.data.access import ReadContext, person
from daimon.data.schema import Schemas
from psycopg import AsyncConnection
from psycopg.rows import DictRow

type Report = dict[str, Any]

STALE_HOURS: Final = 48
"""A source that last synced longer ago than this drifted."""
SPOT: Final = 20
"""Examples listed beside a count."""


@asynccontextmanager
async def _read_only(conn: AsyncConnection[DictRow]) -> AsyncIterator[None]:
    async with conn.transaction():
        await conn.execute(b"set transaction read only")
        yield


async def _all(conn: AsyncConnection[DictRow], sql: str, *params: object) -> list[DictRow]:
    cur = await conn.execute(sql.encode(), params)  # pyright: ignore[reportArgumentType]
    return await cur.fetchall()


async def grants(conn: AsyncConnection[DictRow], schemas: Schemas, tenant: uuid.UUID) -> Report:
    """Each scope with its kind and how many holders it has; scopes nobody holds."""
    d = schemas.data
    async with _read_only(conn):
        rows = await _all(
            conn,
            f"select s.id, s.kind, count(g.id) as holders from {d}.scopes s left join {d}.grants g"
            " on g.tenant_id = s.tenant_id and g.scope = s.id where s.tenant_id = %s"
            " group by s.id, s.kind order by s.id",
            tenant,
        )
    return {
        "scopes": [{"id": r["id"], "kind": r["kind"], "holders": r["holders"]} for r in rows],
        "unheld": [r["id"] for r in rows if not r["holders"]],
    }


async def scopes(conn: AsyncConnection[DictRow], schemas: Schemas, tenant: uuid.UUID) -> Report:
    """Records per source and scope; those in a scope the tenant never defined, which nobody
    holds, and those read through an access list."""
    d = schemas.data
    async with _read_only(conn):
        rows = await _all(
            conn,
            f"select s.name, r.scope, (r.acl is not null) as listed, count(*) as records,"
            f" not exists (select 1 from {d}.scopes c where c.tenant_id = s.tenant_id"
            " and c.id = r.scope) as undefined"
            f" from {d}.records r join {d}.sources s on s.id = r.source_id where s.tenant_id = %s"
            " group by s.name, s.tenant_id, r.scope, listed, undefined order by 1, 2",
            tenant,
        )
    return {
        "records": [dict(r) for r in rows],
        "undefined": [dict(r) for r in rows if r["undefined"] and not r["listed"]],
    }


async def person_access(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant: uuid.UUID, account: uuid.UUID
) -> Report:
    """What one person holds and reads: their holders, scopes and the sources they see."""
    async with _read_only(conn):
        holders = await identity.list_holders(conn, schemas, tenant, account)
        held = await access.effective_scopes(conn, schemas, tenant, holders)
    ctx = ReadContext((tenant,), tuple(held), (person(account),), salt="probe")
    seen = await reads.sources(conn, schemas, ctx)
    return {
        "holders": holders,
        "scopes": held,
        "sources": [dataclasses.asdict(s) for s in seen],
    }


async def health(conn: AsyncConnection[DictRow], schemas: Schemas, tenant: uuid.UUID) -> Report:
    """Each source's state, last sync and how much it holds and has embedded."""
    d = schemas.data
    vectors = (await _all(conn, "select to_regclass(%s) as t", f"{d}.chunk_vectors"))[0]["t"]
    embedded = (
        f"(select count(*) from {d}.chunk_vectors v join {d}.chunks c on c.id = v.chunk_id"
        " where c.source_id = s.id)"
        if vectors
        else "null"
    )
    async with _read_only(conn):
        rows = await _all(
            conn,
            f"select s.id, s.name, s.kind, s.status, s.publication, c.last_sync_at, c.last_status,"
            f" c.errors, left(c.last_error, 200) as last_error,"
            f" (select count(*) from {d}.records r where r.source_id = s.id) as records,"
            f" (select count(*) from {d}.chunks k where k.source_id = s.id) as chunks,"
            f" {embedded} as embedded from {d}.sources s"
            f" left join {d}.source_cursors c on c.source_id = s.id where s.tenant_id = %s"
            " order by s.name",
            tenant,
        )
    return {"sources": [dict(r) for r in rows]}


async def drift(conn: AsyncConnection[DictRow], schemas: Schemas, tenant: uuid.UUID) -> Report:
    """What disagrees with itself: chunks that do not follow their record's scope or list,
    records without chunks, and active published sources not synced lately or failing."""
    d = schemas.data
    async with _read_only(conn):
        chunks = await _all(
            conn,
            f"select c.id from {d}.chunks c join {d}.records r on r.id = c.record_id"
            f" join {d}.sources s on s.id = r.source_id where s.tenant_id = %s"
            " and (c.scope is distinct from r.scope or c.acl is distinct from r.acl) limit %s",
            tenant,
            SPOT,
        )
        bare = await _all(
            conn,
            f"select r.id from {d}.records r join {d}.sources s on s.id = r.source_id"
            f" where s.tenant_id = %s and not exists (select 1 from {d}.chunks c"
            " where c.record_id = r.id) limit %s",
            tenant,
            SPOT,
        )
        stale = await _all(
            conn,
            f"select s.id, s.name, c.last_sync_at, c.last_status from {d}.sources s"
            f" left join {d}.source_cursors c on c.source_id = s.id where s.tenant_id = %s"
            " and s.status = 'active' and s.publication = 'published'"
            " and (c.last_sync_at is null or c.last_sync_at < now() - make_interval(hours => %s)"
            " or c.last_status = 'error') order by s.name",
            tenant,
            STALE_HOURS,
        )
    return {
        "chunks_off_record": [r["id"] for r in chunks],
        "records_without_chunks": [r["id"] for r in bare],
        "sources_not_synced": [dict(r) for r in stale],
    }


NAMES: Final = ("grants", "scopes", "person", "health", "drift")


async def run(
    name: str,
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant: uuid.UUID,
    account: uuid.UUID | None = None,
) -> Report:
    """One probe by name; `person` needs an account."""
    match name:
        case "grants":
            return await grants(conn, schemas, tenant)
        case "scopes":
            return await scopes(conn, schemas, tenant)
        case "person" if account is not None:
            return await person_access(conn, schemas, tenant, account)
        case "person":
            raise ValueError("the person probe needs an account")
        case "health":
            return await health(conn, schemas, tenant)
        case "drift":
            return await drift(conn, schemas, tenant)
        case _:
            raise ValueError(f"unknown probe {name!r}: one of {', '.join(NAMES)}")
