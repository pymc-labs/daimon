"""One search: the arms, reciprocal rank fusion, the fetch and the optional rerank.

Every read runs inside `access.scoped`. The arms run one after another on its connection, each
in a savepoint with a statement timeout, so the time the request has left bounds them and a
cut arm costs only itself. The query's embedding is fetched while full text runs. A payer out
of balance, a model the rate table does not price or an embedder that fails leave the
full-text answer, with a note. With the deployment's `Env`, stored hits whose source re-checks
reads are asked again (`recheck`) and live sources answer beside the rerank (`federated`).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import psycopg
import structlog
from daimon.core.config import DataSettings
from daimon.data import access, embed, federated, recheck
from daimon.data.access import ReadContext
from daimon.data.metering import Meter, Payer, can_spend
from daimon.data.schema import Schemas
from daimon.data.search import arms, rerank, timewin
from daimon.data.search.arms import Filters, Row
from daimon.data.sync.engine import Env
from psycopg import AsyncConnection
from psycopg.rows import DictRow

log = structlog.get_logger()

RRF_K: Final = 60
BUDGET: Final = {"low": 20, "mid": 60, "high": 150}
"""Rows per arm."""
RERANK_TOP: Final = 40
"""The fused candidates fetched and reranked."""
FETCH_SECONDS: Final = 3.0
SNIPPET: Final = "MaxFragments=1, MaxWords=40, MinWords=15, StartSel=<<, StopSel=>>"


@dataclass(frozen=True)
class Hit:
    record_id: UUID
    chunk_id: UUID
    source_id: UUID
    external_id: str
    kind: str
    title: str
    url: str | None
    occurred_at: datetime
    scope: str
    text: str
    snippet: str
    score: float
    arms: dict[str, int]
    """The rank each arm gave it."""


@dataclass
class Result:
    hits: list[Hit]
    notes: list[str] = field(default_factory=list[str])
    """What did not run or was cut, as short phrases: `balance`, `embedding`, `timeout`, ..."""
    rerank: str = "off"
    window: timewin.Window | None = None
    live: list[federated.LiveResult] = field(default_factory=list[federated.LiveResult])
    """One per live source the caller may read (`federated.search`)."""


def fuse(lists: dict[str, list[Row]], k: int = RRF_K) -> list[dict[str, Any]]:
    """Reciprocal rank fusion of ranked rows with an `id`. Best first; equal scores by the
    better best rank, then by id. Each carries the row of the arm that ranked it highest."""
    out: dict[str, dict[str, Any]] = {}
    for arm, rows in lists.items():
        seen: set[str] = set()
        for row in rows:
            rid = str(row["id"])
            if rid in seen:
                continue
            seen.add(rid)
            r = len(seen)
            c = out.setdefault(rid, {"id": rid, "rrf": 0.0, "arms": {}, "row": row, "best": r})
            c["rrf"] += 1 / (k + r)
            c["arms"][arm] = r
            if r < c["best"]:
                c["row"], c["best"] = row, r
    return sorted(out.values(), key=lambda c: (-c["rrf"], c["best"], c["id"]))


def _left(end: float) -> float:
    return max(0.0, end - time.monotonic())


async def _arm(
    conn: AsyncConnection[DictRow],
    end: float,
    notes: list[str],
    name: str,
    work: Callable[[], Awaitable[list[Row]]],
) -> list[Row] | None:
    """One arm in a savepoint under what is left of the request's time; None, with a note,
    when it ran out or failed."""
    if _left(end) <= 0:
        notes.append(f"{name}: timeout")
        return None
    try:
        async with conn.transaction():
            await conn.execute(
                f"set local statement_timeout = {max(100, int(_left(end) * 1000))}".encode()
            )
            return await work()
    except psycopg.errors.QueryCanceled:
        notes.append(f"{name}: timeout")
    except psycopg.Error:
        log.warning("data.search_arm_failed", arm=name, exc_info=True)
        notes.append(f"{name}: failed")
    return None


async def search(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    ctx: ReadContext,
    text: str,
    *,
    settings: DataSettings,
    meter: Meter | None = None,
    payer: Payer | None = None,
    limit: int = 10,
    filters: Filters | None = None,
    budget: str = "mid",
    now: datetime | None = None,
    use_vectors: bool = True,
    use_rerank: bool = True,
    env: Env | None = None,
    live_meter: federated.Meter | None = None,
) -> Result:
    """The best `limit` chunks (one per record) for `text`, as `ctx` may read them.

    `conn` is the store owner's autocommit connection, as `access.scoped` takes. The query's
    embedding and the rerank are billed to `payer` through `meter` (neither, when there is
    no meter or payer): a payer out of balance gets full text only. Spend is recorded as
    soon as the provider answers, whatever happens to the rest of the search. With `env`,
    hits pass `recheck.allowed` and live sources are asked, each call through `live_meter`.
    """
    end = time.monotonic() + settings.search_seconds
    notes: list[str] = []
    depth = BUDGET.get(budget, BUDGET["mid"])
    win = timewin.parse(text, now or datetime.now(UTC))
    q = timewin.strip(text, win)
    ns = await embed.vector_schema(conn)
    vectors = use_vectors and embed.configured(settings) and ns is not None
    if vectors and meter is not None and not meter.priced(settings.embed_model):
        vectors = False
        notes.append("embedding: unpriced")
    if vectors and not await can_spend(meter, payer):
        vectors = False
        notes.append("balance")
    # The API round trip runs beside full text.
    vt = asyncio.ensure_future(embed.embed(settings, [q], query=True)) if vectors else None
    lists: dict[str, list[Row]] = {}
    bm25_ns: str | None = None
    try:
        async with access.scoped(conn, schemas, ctx):
            args: dict[str, Any] = {}
            f = arms.where(filters or Filters(), args, ctx.scopes)
            tsq, words = arms.terms(q)
            cur = await conn.execute(
                b"select extnamespace::regnamespace::text as ns from pg_extension"
                b" where extname = 'pg_textsearch'"
            )
            if row := await cur.fetchone():
                bm25_ns = row["ns"]
            rows = await _arm(
                conn,
                end,
                notes,
                "fts",
                lambda: arms.fts(conn, schemas, tsq, words, depth, f, args, bm25_ns),
            )
            if rows is None and bm25_ns is not None:  # BM25 failed: rank by ts_rank_cd instead
                notes.pop()
                rows = await _arm(
                    conn,
                    end,
                    notes,
                    "fts",
                    lambda: arms.fts(conn, schemas, tsq, words, depth, f, args, None),
                )
            if rows is not None:
                lists["fts"] = rows
            v = await _embedded(vt, end, notes, meter, payer, settings) if vt else None
            if v is not None and ns is not None:
                dim = settings.embed_dim

                async def nearest() -> list[Row]:
                    await arms.tune(conn, schemas, depth)
                    return await arms.vec(conn, schemas, ns, dim, v, depth, f, args)

                async def dated() -> list[Row]:
                    assert win is not None
                    return await arms.time_window(conn, schemas, ns, dim, v, win, depth, f, args)

                if (rows := await _arm(conn, end, notes, "vec", nearest)) is not None:
                    lists["vec"] = rows
                if win is not None and (rows := await _arm(conn, end, notes, "time", dated)):
                    lists["time"] = rows
            fused = fuse(lists)[: max(RERANK_TOP, limit)]
            hits = await _fetch(conn, schemas, fused, tsq, notes)
    finally:
        if vt is not None and not vt.done():
            vt.cancel()
    if env is not None and hits:
        ok = await recheck.allowed(
            conn, schemas, env, ctx.readers, [(h.source_id, h.external_id) for h in hits],
            seconds=max(1.0, _left(end)),
        )  # fmt: skip
        hits = [h for h in hits if (h.source_id, h.external_id) in ok]
    status = "off"
    # The live sources answer beside the rerank, and never outlive this search's connection.
    try:
        async with asyncio.TaskGroup() as tg:
            live = (
                tg.create_task(
                    federated.search(
                        conn, schemas, env, ctx, q, seconds=max(1.0, _left(end)), meter=live_meter
                    )
                )
                if env is not None
                else None
            )
            if use_rerank and hits:
                order, status = await rerank.order(
                    settings,
                    text,
                    [h.text for h in hits[:RERANK_TOP]],
                    meter=meter,
                    payer=payer,
                    seconds=max(1.0, _left(end)),
                )
                if order is not None:
                    head = hits[:RERANK_TOP]
                    hits = [head[i] for i in order] + hits[RERANK_TOP:]
    except ExceptionGroup as eg:  # a failed arm fails the search as itself
        raise eg.exceptions[0] from None
    return Result(hits[:limit], notes, status, win, live.result() if live is not None else [])


async def _embedded(
    vt: asyncio.Future[tuple[list[list[float]], int]],
    end: float,
    notes: list[str],
    meter: Meter | None,
    payer: Payer | None,
    settings: DataSettings,
) -> str | None:
    """The query's vector literal and its billing; None, with a note, when it did not come."""
    try:
        vs, tokens = await asyncio.wait_for(asyncio.shield(vt), _left(end))
    except TimeoutError:
        notes.append("embedding: timeout")
        return None
    except embed.EmbedError:
        log.warning("data.query_embedding_failed", exc_info=True)
        notes.append("embedding: failed")
        return None
    if meter is not None and payer is not None:
        try:
            await meter.record(
                "data_embedding",
                tenant_id=payer.tenant_id,
                platform_user_id=payer.platform_user_id,
                model=settings.embed_model,
                units=tokens,
            )
        except Exception:
            log.exception("data.query_embedding_unbilled", tenant=str(payer.tenant_id))
    return embed.vec(vs[0])


async def _fetch(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    fused: list[dict[str, Any]],
    tsq: str,
    notes: list[str],
) -> list[Hit]:
    """The fused candidates as hits, in fused order."""
    if not fused:
        return []
    d = schemas.data
    try:
        async with conn.transaction():
            await conn.execute(
                f"set local statement_timeout = {int(FETCH_SECONDS * 1000)}".encode()
            )
            cur = await conn.execute(
                f"select c.id, c.record_id, c.source_id, r.external_id, c.text, c.ctx, c.scope,"
                f" r.kind, r.title,"
                f" r.url, r.occurred_at, ts_headline('english', c.text,"
                f" to_tsquery('english', %s), %s) as snippet"
                f" from {d}.chunks c join {d}.records r on r.id = c.record_id"
                " where c.id = any(%s)".encode(),
                (tsq, SNIPPET, [c["row"]["chunk"] for c in fused]),
            )
            rows = {r["id"]: r for r in await cur.fetchall()}
    except psycopg.Error:
        log.warning("data.search_fetch_failed", exc_info=True)
        notes.append("fetch: failed")
        return []
    out: list[Hit] = []
    for c in fused:
        r = rows.get(c["row"]["chunk"])
        if r is not None:  # gone since the arm ran
            out.append(
                Hit(
                    r["record_id"], r["id"], r["source_id"], r["external_id"], r["kind"],
                    r["title"], r["url"],
                    r["occurred_at"], r["scope"], r["text"], r["snippet"], c["rrf"], c["arms"],
                )
            )  # fmt: skip
    return out
