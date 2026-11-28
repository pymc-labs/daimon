"""The embeddings client: an OpenAI-compatible `/embeddings` endpoint.

`DAIMON_DATA__EMBED_PROVIDER` is `openai` (or empty) for a plain endpoint, or the providers an
OpenRouter-style router may use, comma-separated, tried in order and with data collection
refused. Vectors are cut to `EMBED_DIM` and normalised, so a Matryoshka model's longer vector
fits the store's index.
"""

# pyright: basic
from __future__ import annotations

import asyncio
import math
from collections.abc import Iterator, Sequence
from typing import Any, Final

import httpx
import structlog
from daimon.core.config import DataSettings
from daimon.data.locks import advisory_key
from daimon.data.schema import Schemas
from psycopg import AsyncConnection
from psycopg.rows import DictRow

log = structlog.get_logger()

API_BATCH: Final = 128
API_MAX_CHARS: Final = 20000
"""About 6k tokens: under the common 8k input limit."""
API_BATCH_CHARS: Final = 60000
"""A request's total, under 120k-token caps even at one token per character."""
WAITS: Final = (1, 2, 4, 8, 16)
"""Back-off between tries on 429, 5xx, network errors and answers missing embeddings."""
QUERY_WAITS: Final = (1, 2)
"""A search waits seconds, not minutes."""
MAX_INDEXED_DIM: Final = 2000
"""The most dimensions pgvector's HNSW indexes for the `vector` type."""

transport: httpx.AsyncBaseTransport | None = None
"""Tests answer for the endpoint here."""


class EmbedError(RuntimeError):
    """The endpoint could not embed; the message holds no provider text beyond a status."""


def configured(settings: DataSettings) -> bool:
    return bool(settings.embed_url and settings.embed_model)


def _routing(provider: str) -> dict[str, Any] | None:
    names = [p.strip() for p in provider.split(",") if p.strip() and p.strip() != "openai"]
    if not names:
        return None
    return {
        "order": names,
        "only": names,
        "allow_fallbacks": False,
        "zdr": True,
        "data_collection": "deny",
    }


def _batches(texts: Sequence[str]) -> Iterator[list[str]]:
    """Requests within API_BATCH texts and API_BATCH_CHARS characters (a longer text goes alone)."""
    part: list[str] = []
    size = 0
    for t in texts:
        if part and (len(part) == API_BATCH or size + len(t) > API_BATCH_CHARS):
            yield part
            part, size = [], 0
        part.append(t)
        size += len(t)
    if part:
        yield part


def _fit(v: list[float], dim: int) -> list[float]:
    """The first `dim` dimensions, renormalised (what `dimensions` does server-side)."""
    if len(v) < dim:
        raise EmbedError(f"the embedding model returns {len(v)} dimensions; the store keeps {dim}")
    v = v[:dim]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


async def _post(
    http: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    waits: tuple[int, ...],
) -> tuple[list[dict[str, Any]], Any]:
    """One request's embeddings in input order, and the usage the provider reports. 429, 5xx,
    network errors and answers missing embeddings are retried after each wait."""
    err = ""
    for wait in (*waits, None):
        try:
            r = await http.post(url, json=body, headers=headers)
            data = r.json().get("data") if r.status_code == 200 else None
            if (
                isinstance(data, list)
                and len(data) == len(body["input"])
                and all(isinstance(d, dict) and isinstance(d.get("embedding"), list) for d in data)
            ):
                return sorted(data, key=lambda d: d.get("index", 0)), r.json().get("usage")
            err = f"embeddings API answered {r.status_code}"
            if r.status_code not in (200, 429) and r.status_code < 500:
                raise EmbedError(err)
        except httpx.TransportError as e:
            err = f"embeddings API: {type(e).__name__}"
        except ValueError:
            err = "embeddings API answered without JSON"
        if wait is None:
            raise EmbedError(err)
        await asyncio.sleep(wait)
    raise EmbedError(err)  # unreachable: the last wait is None


async def embed(
    settings: DataSettings, texts: Sequence[str], *, query: bool = False
) -> tuple[list[list[float]], int]:
    """The vectors of `texts` and the input tokens the provider counted (an estimate of one per
    four characters when it counts none). A query gets the query prefix and fewer retries."""
    if not configured(settings):
        raise EmbedError("no embeddings endpoint is configured")
    prefix = settings.embed_query_prefix if query else ""
    cleaned = [(prefix + t)[:API_MAX_CHARS] if t.strip() else " " for t in texts]
    body: dict[str, Any] = {"model": settings.embed_model}
    if (route := _routing(settings.embed_provider)) is not None:
        body["provider"] = route
    key = settings.embed_key.get_secret_value() if settings.embed_key else ""
    headers = {"Authorization": f"Bearer {key}"}
    url = f"{(settings.embed_url or '').rstrip('/')}/embeddings"
    out: list[list[float]] = []
    tokens = 0
    async with httpx.AsyncClient(timeout=10 if query else 30, transport=transport) as http:
        for part in _batches(cleaned):
            data, usage = await _post(
                http, url, headers, {**body, "input": part}, QUERY_WAITS if query else WAITS
            )
            out += [_fit(d["embedding"], settings.embed_dim) for d in data]
            used = (
                usage.get("prompt_tokens") or usage.get("total_tokens")
                if isinstance(usage, dict)
                else None
            )
            tokens += int(used) if used else sum(len(t) for t in part) // 4 + len(part)
    return out, tokens


def vec(v: Sequence[float]) -> str:
    """pgvector text literal."""
    return "[" + ",".join(f"{x:.5f}" for x in v) + "]"


async def vector_schema(conn: AsyncConnection[DictRow]) -> str | None:
    """The schema of the `vector` extension, or None while it is not installed (the store then
    answers by full text alone)."""
    cur = await conn.execute(
        b"select extnamespace::regnamespace::text as ns from pg_extension where extname = 'vector'"
    )
    row = await cur.fetchone()
    return row["ns"] if row else None


async def ensure_index(conn: AsyncConnection[DictRow], schemas: Schemas, dim: int, ns: str) -> bool:
    """The HNSW index over vectors cut to `dim`, built without blocking writes. False when
    there is none (more dimensions than pgvector indexes: search reads exactly). The index
    is on the cast, so a query must use `cast_sql` to reach it."""
    if dim > MAX_INDEXED_DIM:
        return False
    d, name = schemas.data, f"chunk_vectors_hnsw_{dim}"
    key = advisory_key(d, f"index:{dim}")
    got = await (await conn.execute(b"select pg_try_advisory_lock(%s) as ok", (key,))).fetchone()
    if not (got and got["ok"]):
        return False  # another worker builds it; an invalid index meanwhile is its build
    try:
        cur = await conn.execute(
            b"select indisvalid as ok from pg_index where indexrelid = to_regclass(%s)",
            (f"{d}.{name}",),
        )
        if (row := await cur.fetchone()) and not row["ok"]:  # a cancelled build's leftover
            await conn.execute(f"drop index concurrently {d}.{name}".encode())
        await conn.execute(
            f"create index concurrently if not exists {name} on {d}.chunk_vectors"
            f" using hnsw (({cast_sql(ns, dim, 'embedding')}) {ns}.vector_cosine_ops)".encode()
        )
    except Exception:
        log.warning("data.index_failed", dim=dim, exc_info=True)
        return False
    finally:
        await conn.execute(b"select pg_advisory_unlock(%s)", (key,))
    return True


def cast_sql(ns: str, dim: int, column: str = "v.embedding") -> str:
    return f"{column}::{ns}.vector({dim})"
