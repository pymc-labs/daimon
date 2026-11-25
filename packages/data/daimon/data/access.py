"""Who reads, and the transaction that reads as them.

A read runs inside `scoped`: the store's owner writes the caller's `ReadContext` for this
transaction, fills the audience's visible access lists, then switches to the
`daimon_data_reader` role, which sees rows only through the policies of `0001_store.sql`.
Without a context that role sees nothing. The context is the caller's whole input: callers
resolve who reads (the data audience) before they open one, and the model never supplies it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

from daimon.core.ports.data_subject import subject as subject
from daimon.data.schema import Schemas
from psycopg import AsyncConnection
from psycopg.rows import DictRow

READER_CAP: Final = 200
"""Readers in one audience; more, or none, read no list rows."""
POLICY: Final = 1
"""Part of every audience key: bump it to refill every audience."""
ACL_SCOPE: Final = "restricted:acl"
"""The scope of rows read through their access list only; no context ever holds it."""


def person(account_id: uuid.UUID) -> str:
    """A Daimon account as a reader or a grant holder."""
    return f"account:{account_id}"


@dataclass(frozen=True)
class ReadContext:
    """Everything a read may see, fixed before it starts.

    `tenant_ids`: whose sources are visible. `scopes`: scopes every reader holds (rows
    without a list). `readers`: `person(...)` or `subject(...)` values; a list row shows when
    every reader holds its list. `personal_ok`: false hides one person's private records
    (an agent reading for others). `limits`: the list ids the caller may reach at most
    (None: no limit). `salt`: what else tells this caller apart (an agent, a key).
    """

    tenant_ids: tuple[uuid.UUID, ...]
    scopes: tuple[str, ...] = ()
    readers: tuple[str, ...] = ()
    personal_ok: bool = True
    limits: tuple[int, ...] | None = None
    salt: str = ""


def _arguments(ctx: ReadContext) -> dict[str, object]:
    return {
        "tenants": sorted(set(ctx.tenant_ids)),
        "scopes": sorted(set(ctx.scopes)),
        "readers": json.dumps(sorted(set(ctx.readers))),
        "personal": ctx.personal_ok,
        "limits": list(ctx.limits) if ctx.limits is not None else None,
        "salt": f"{ctx.salt}|{POLICY}",
    }


async def fill(conn: AsyncConnection[DictRow], schemas: Schemas, ctx: ReadContext) -> int:
    """Fill the audience's visible lists in a short transaction of its own (`conn` in
    autocommit), so a concurrent read of the same audience waits only milliseconds."""
    if not ctx.readers:
        return 0
    async with conn.transaction():
        cur = await conn.execute(
            f"select {schemas.data}.fill_audience(%(readers)s::jsonb, %(personal)s,"
            " %(limits)s::int[], %(salt)s, %(tenants)s::uuid[]) as n".encode(),
            _arguments(ctx),
        )
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


@asynccontextmanager
async def scoped(
    conn: AsyncConnection[DictRow], schemas: Schemas, ctx: ReadContext
) -> AsyncIterator[AsyncConnection[DictRow]]:
    """A read transaction on `conn` (autocommit, owned by the store's owner) that sees only
    what `ctx` allows. The caller must not run SQL it did not write inside it."""
    await fill(conn, schemas, ctx)
    async with conn.transaction():
        await conn.execute(
            f"insert into {schemas.data}.read_contexts"
            " (pid, xact, tenant_ids, scopes, readers, personal_ok, limits, salt, key)"
            " select pg_backend_pid(), pg_current_xact_id(), %(tenants)s::uuid[],"
            " %(scopes)s::text[], %(readers)s::jsonb, %(personal)s, %(limits)s::int[], %(salt)s,"
            f" {schemas.data}.audience_key_for(%(readers)s::jsonb, %(personal)s, %(limits)s::int[],"
            " %(salt)s, %(tenants)s::uuid[])"
            " on conflict (pid) do update set xact = excluded.xact,"
            " tenant_ids = excluded.tenant_ids, scopes = excluded.scopes,"
            " readers = excluded.readers, personal_ok = excluded.personal_ok,"
            " limits = excluded.limits, salt = excluded.salt, key = excluded.key".encode(),
            _arguments(ctx),
        )
        await conn.execute(f"select {schemas.data}.fill_audience()".encode())
        await conn.execute(b"set local role daimon_data_reader")
        yield conn


async def effective_scopes(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID, holders: Iterable[str]
) -> list[str]:
    """The scopes granted in a tenant to any of `holders` (a person and their roles)."""
    cur = await conn.execute(
        f"select distinct scope from {schemas.data}.grants"
        " where tenant_id = %s and holder = any(%s) order by scope".encode(),
        (tenant_id, list(holders)),
    )
    return [str(r["scope"]) for r in await cur.fetchall()]
