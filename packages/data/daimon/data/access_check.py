"""`daimon data access-check`: who can read a source or a record, and why.

Reads the store as its owner and, for one person, runs the real read path (`access.scoped`)
so the answer is the one a reader would get. Lines are plain text for an operator.
"""

from __future__ import annotations

import uuid
from typing import Final

from daimon.data import freshness
from daimon.data.access import ReadContext, effective_scopes, person, scoped
from daimon.data.schema import Schemas
from psycopg import AsyncConnection
from psycopg.rows import DictRow

SHOWN: Final = 40
"""Holders listed before the rest are counted."""


async def _holders(conn: AsyncConnection[DictRow], d: str, acl_id: int, kind: str) -> list[str]:
    """Who holds an access list now: the holders every one of its leaves grants, on a live unit."""
    leaves = [acl_id]
    if kind == "all":
        cur = await conn.execute(
            f"select children from {d}.acl_conjunctions where acl_id = %s".encode(), (acl_id,)
        )
        row = await cur.fetchone()
        leaves = list(row["children"]) if row else []
    cur = await conn.execute(
        f"select g.holder from {d}.acl_grants g left join {d}.evidence_units u on u.id = g.unit_id"
        " where g.acl_id = any(%s) and (g.unit_id is null or u.expires_at > statement_timestamp())"
        " group by g.holder having count(distinct g.acl_id) = %s order by g.holder".encode(),
        (leaves, len(leaves)),
    )
    return [r["holder"] for r in await cur.fetchall()]


async def _who(conn: AsyncConnection[DictRow], d: str, tenant: uuid.UUID, holder: str) -> str:
    """A holder as an operator reads it: the account behind a source subject, when linked."""
    cur = await conn.execute(
        f"select account_id from {d}.identities where tenant_id = %s and account_id is not null"
        " and provider || '/' || workspace || ':user:' || subject = %s".encode(),
        (tenant, holder),
    )
    row = await cur.fetchone()
    return f"{holder} (account {row['account_id']})" if row else holder


async def check(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    record: str | None = None,
    account: uuid.UUID | None = None,
) -> list[str]:
    """What an operator needs to know to explain a read: the source's state, the record's
    list and holders, and whether `account` can read it now."""
    d = schemas.data
    cur = await conn.execute(
        f"select name, kind, status, publication, mode, bound, default_scope from {d}.sources"
        " where id = %s and tenant_id = %s".encode(),
        (source_id, tenant_id),
    )
    src = await cur.fetchone()
    if src is None:
        return ["no such source in this workspace"]
    fresh = await freshness.state(conn, schemas, source_id)
    lines = [
        f"source {src['name']} ({src['kind']}): publication {src['publication']},"
        f" status {src['status']}" + (", declared" if src["bound"] else ""),
    ]
    why: str | None = None
    if src["publication"] != "published":
        why = f"the source is {src['publication']}, not published"
    elif src["status"] == "disabled":
        why = "the source is disabled"
    elif fresh and not fresh.fresh:
        why = "the source is hidden: its last complete sync is older than its freshness limit"
    if fresh and fresh.lifetime:
        lines.append(f"freshness: last complete sync {fresh.last_sync_at}, limit {fresh.lifetime}")
    if src["mode"] == "federated":
        lines.append(
            f"live source: read upstream, answers callers holding scope {src['default_scope']}"
        )
    if record is not None and src["mode"] == "stored":
        cur = await conn.execute(
            f"select r.scope, r.acl, a.kind, a.personal, coalesce(x.held_at is not null"
            f" or x.until <= statement_timestamp(), false) as lapsed from {d}.records r"
            f" left join {d}.acls a on a.id = r.acl left join {d}.record_access x"
            " on x.source_id = r.source_id and x.external_id = r.external_id"
            " where r.source_id = %s and r.external_id = %s".encode(),
            (source_id, record),
        )
        rec = await cur.fetchone()
        if rec is None:
            return [*lines, "no such record in this source"]
        if rec["acl"] is None:
            cur = await conn.execute(
                f"select holder from {d}.grants where tenant_id = %s and scope = %s"
                " order by holder".encode(),
                (tenant_id, rec["scope"]),
            )
            holders = [r["holder"] for r in await cur.fetchall()]
            lines.append(f"record read by scope {rec['scope']}; granted to {len(holders)} holders")
        else:
            holders = await _holders(conn, d, rec["acl"], rec["kind"])
            lines.append(
                f"record on access list {rec['acl']} ({rec['kind']}"
                + (", personal" if rec["personal"] else "")
                + f"); {len(holders)} holders"
            )
            if rec["lapsed"]:
                lines.append("the record's evidence has lapsed: unread until the next sync")
                why = why or "the record's evidence has lapsed"
        lines += [f"  {await _who(conn, d, tenant_id, h)}" for h in holders[:SHOWN]]
        if len(holders) > SHOWN:
            lines.append(f"  and {len(holders) - SHOWN} more")
    if account is not None:
        reader = person(account)
        scopes = await effective_scopes(conn, schemas, tenant_id, [reader])
        ctx = ReadContext(tenant_ids=(tenant_id,), scopes=tuple(scopes), readers=(reader,))
        async with scoped(conn, schemas, ctx):
            sql = (
                f"select count(*) as n from {d}.records where source_id = %s and external_id = %s"
                if record is not None
                else f"select count(*) as n from {d}.sources where id = %s"
            )
            cur = await conn.execute(
                sql.encode(), (source_id, record) if record is not None else (source_id,)
            )
            row = await cur.fetchone()
        readable = bool(row and row["n"])
        lines.append(f"account {account} {'can' if readable else 'cannot'} read it now")
        if not readable:
            lines.append(f"why not: {why or 'no list or scope the person holds covers it'}")
    return lines
