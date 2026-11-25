"""`data_sql`: read-only SQL over the store's views, for a caller who wrote the text.

The text never runs as the reader or the store's owner. It is the body of `caller_sql`, a
function owned by `daimon_data_sql`, a role that reaches the views schema and nothing else:
no table, no access list, no other schema. Postgres keeps a security-definer function's role
for its whole body and refuses `SET ROLE` and `RESET ROLE` inside it, so the text cannot get
back to the reader or the owner. The views are owned by `daimon_data_view_owner` and apply
the same row policies as every read. A read context still wraps the call, so those policies
know who is asking.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Final

import psycopg
from daimon.data import access
from daimon.data.access import ReadContext
from daimon.data.schema import Schemas
from psycopg import AsyncConnection
from psycopg.rows import DictRow

LIMIT: Final = 500
SECONDS: Final = 5
"""The server's statement timeout; the wait is one second longer, as the text may lift the first."""


class SqlRefused(ValueError):
    """The text or the store is not fit to run it; the message is for the caller."""


@dataclass(frozen=True)
class SqlResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool


async def _guarded(conn: AsyncConnection[DictRow], schemas: Schemas) -> None:
    """Refuse while the views or the function have the wrong owner, as after a restore that
    dropped ownership: `daimon data init --repair` puts it back."""
    cur = await conn.execute(
        b"select (select count(*) from pg_views where schemaname = %s"
        b" and viewowner <> 'daimon_data_view_owner') as views,"
        b" (select pg_get_userbyid(proowner) from pg_proc"
        b"  where oid = to_regprocedure(%s)) as owner",
        (schemas.views, f"{schemas.data}.caller_sql(text, int)"),
    )
    row = await cur.fetchone()
    if row is None or row["views"] or row["owner"] != "daimon_data_sql":
        raise SqlRefused("the store's read views are not set up; run `daimon data init --repair`")


async def data_sql(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    ctx: ReadContext,
    sql: str,
    limit: int = LIMIT,
) -> SqlResult:
    """Rows of one `select` (or `with`, `table`, `values`) over the views, as `ctx` may read."""
    text = sql.strip().rstrip(";").strip()
    if ";" in text:
        raise SqlRefused("one statement only")
    if not re.match(r"(select|with|table|values)\b", text, re.I):
        raise SqlRefused("queries only: start with select or with")
    await _guarded(conn, schemas)
    limit = min(max(limit, 1), LIMIT)
    try:
        async with access.scoped(conn, schemas, ctx) as c:
            await c.execute(b"set transaction read only")
            await c.execute(f"set local statement_timeout = '{SECONDS}s'".encode())
            async with asyncio.timeout(SECONDS + 1):
                cur = await c.execute(
                    f"select {schemas.data}.caller_sql(%s, %s) as r".encode(), (text, limit)
                )
                rows = [r["r"] for r in await cur.fetchall()]
    except (TimeoutError, psycopg.errors.QueryCanceled):
        raise SqlRefused("the query timed out") from None
    except psycopg.Error as e:
        raise SqlRefused(e.diag.message_primary or "the query failed") from None
    return SqlResult(list(rows[0]) if rows else [], rows, len(rows) == limit)
