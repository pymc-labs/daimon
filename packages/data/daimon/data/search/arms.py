"""The stored arms: each returns ranked `(record, chunk)` rows for one reader.

They run on a connection inside `access.scoped`, so every row is one the reader may see; none
of them names an access column. The vector arm is the one that needs the `vector` extension,
the BM25 order the one that needs `pg_textsearch`.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from daimon.data.embed import cast_sql
from daimon.data.schema import Schemas
from daimon.data.search.timewin import Window
from psycopg import AsyncConnection
from psycopg.rows import DictRow

type Row = dict[str, Any]

EF_BY_SHARE: Final = ((0.2, 200), (0.03, 400), (0.0, 800))
"""HNSW ef_search by the share of access-listed rows the reader may see: the index walks
ef_search candidates and then filters them, so a reader of few rows needs a wider walk."""


@dataclass(frozen=True)
class Filters:
    sources: tuple[UUID, ...] = ()
    kinds: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    since: datetime | None = None
    until: datetime | None = None


def where(f: Filters, args: dict[str, Any], mine: tuple[str, ...]) -> str:
    """The filters as ` and ...` over chunks `c` and records `r`; fills `args`. A scope filter
    leaves rows read by access list alone, as their scope is not one the reader holds."""
    sql: list[str] = []
    if f.sources:
        sql.append("c.source_id = any(%(sources)s)")
        args["sources"] = list(f.sources)
    if f.kinds:
        sql.append("r.kind = any(%(kinds)s)")
        args["kinds"] = list(f.kinds)
    if f.scopes:
        sql.append("(c.scope = any(%(fscopes)s) or c.scope <> all(%(mine)s))")
        args["fscopes"], args["mine"] = list(f.scopes), list(mine)
    if f.since:
        sql.append("r.occurred_at >= %(since)s")
        args["since"] = f.since
    if f.until:
        sql.append("r.occurred_at < %(until)s")
        args["until"] = f.until
    return "".join(f" and {s}" for s in sql)


def terms(query: str) -> tuple[str, str]:
    """`(tsquery, words)`: the query's words as an any-word tsquery (prompts are prose, not
    keywords) and as plain text for BM25."""
    words = [w for w in dict.fromkeys(re.findall(r"[a-z0-9]+", query.lower())) if len(w) > 1]
    return " | ".join(words) or "_", " ".join(words)


async def fts(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    q: str,
    words: str,
    n: int,
    f: str,
    args: dict[str, Any],
    bm25: str | None,
) -> list[Row]:
    """Chunks matching any word, best first: by BM25 where `bm25` is the extension's schema
    and its index is valid, else by ts_rank_cd. Equal ranks fall back to chunk id."""
    d = schemas.data
    order = "ts_rank_cd(c.tsv, to_tsquery('english', %(q)s)) desc, c.id"
    if bm25 is not None:
        cur = await conn.execute(
            b"select indisvalid from pg_index where indexrelid = to_regclass(%s)",
            (f"{d}.chunks_bm25",),
        )
        if (row := await cur.fetchone()) and row["indisvalid"]:
            order = (
                f"c.text operator({bm25}.<@>) {bm25}.to_bm25query(%(w)s, '{d}.chunks_bm25'), c.id"
            )
    cur = await conn.execute(
        f"select c.record_id as id, c.id as chunk from {d}.chunks c"
        f" join {d}.records r on r.id = c.record_id"
        f" where c.tsv @@ to_tsquery('english', %(q)s){f} order by {order} limit %(n)s".encode(),
        {**args, "q": q, "w": words, "n": n},
    )
    return list(await cur.fetchall())


def ef_for_share(share: float, depth: int) -> int:
    base = min(1000, max(40, depth * 2))
    if not 0 < share < 1:
        return base
    return max(base, next(ef for floor, ef in EF_BY_SHARE if share >= floor))


async def tune(conn: AsyncConnection[DictRow], schemas: Schemas, depth: int) -> None:
    """Per-transaction HNSW settings for this reader: a wider walk for a small audience, and
    iterative scans (pgvector 0.8) so a filtered query still finds enough rows."""
    cur = await conn.execute(f"select {schemas.data}.visible_fraction() as f".encode())
    row = await cur.fetchone()
    share = float(row["f"]) if row and row["f"] is not None else 1.0
    await conn.execute(f"set local hnsw.ef_search = {ef_for_share(share, depth)}".encode())
    cur = await conn.execute(b"select extversion from pg_extension where extname = 'vector'")
    row = await cur.fetchone()
    if row and tuple(int(x) for x in re.findall(r"\d+", row["extversion"])[:2]) >= (0, 8):
        await conn.execute(b"set local hnsw.iterative_scan = strict_order")


def settled(rows: list[Row]) -> list[Row]:
    """Equal distances in chunk id order (the SQL orders by distance alone, so its index
    is used); a chunk with no distance last."""
    return sorted(rows, key=lambda r: (r["d"] is None, r["d"] or 0.0, str(r["chunk"])))


async def vec(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    ns: str,
    dim: int,
    v: str,
    n: int,
    f: str,
    args: dict[str, Any],
) -> list[Row]:
    """The nearest chunks by cosine distance, through the HNSW index when it exists."""
    d, dist = schemas.data, f"{cast_sql(ns, dim)} <=> %(v)s::{ns}.vector({dim})"
    cur = await conn.execute(
        f"select c.record_id as id, c.id as chunk, {dist} as d from {d}.chunk_vectors v"
        f" join {d}.chunks c on c.id = v.chunk_id join {d}.records r on r.id = c.record_id"
        f" where true{f} order by {dist} limit %(n)s".encode(),
        {**args, "v": v, "n": n},
    )
    return settled(list(await cur.fetchall()))


async def time_window(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    ns: str,
    dim: int,
    v: str,
    win: Window,
    n: int,
    f: str,
    args: dict[str, Any],
) -> list[Row]:
    """Chunks inside the question's date window by similarity, spread across the window so
    one busy day cannot fill the arm."""
    d, dist = schemas.data, f"{cast_sql(ns, dim)} <=> %(v)s::{ns}.vector({dim})"
    cur = await conn.execute(
        f"select c.record_id as id, c.id as chunk, r.occurred_at, {dist} as d"
        f" from {d}.chunk_vectors v join {d}.chunks c on c.id = v.chunk_id"
        f" join {d}.records r on r.id = c.record_id"
        " where r.occurred_at >= coalesce(%(ws)s::timestamptz, '-infinity')"
        f" and r.occurred_at < coalesce(%(we)s::timestamptz, 'infinity'){f}"
        f" order by {dist} limit %(n)s * 3".encode(),
        {**args, "v": v, "n": n, "ws": win.start, "we": win.end},
    )
    return spread(settled(list(await cur.fetchall())), win, n)


def spread(rows: list[Row], win: Window, n: int, buckets: int = 8) -> list[Row]:
    """Similarity order, but on a first pass no time bucket gets more than its share."""
    if not (win.start and win.end):
        return rows[:n]
    width, cap = (win.end - win.start) / buckets, max(2, n // buckets)
    first: list[Row] = []
    rest: list[Row] = []
    used: Counter[int] = Counter()
    for r in rows:
        b = int((r["occurred_at"] - win.start) / width)
        (first if used[b] < cap else rest).append(r)
        used[b] += 1
    return (first + rest)[:n]
