"""Embedding what sync stored.

A run leaves a chunk without a vector; the embedder fills the gap afterwards, outside any
write transaction, so a sync never waits on a model. The sync loop calls `embed_pending`
after each run and once a day for gaps, only when an embedding endpoint is configured.
Each batch is billed to the tenant of the source it came from; a tenant out of balance
keeps its gaps until it tops up.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from daimon.core.config import DataSettings
from daimon.data import embed
from daimon.data.metering import Meter
from daimon.data.schema import Schemas
from psycopg import AsyncConnection
from psycopg.rows import DictRow

log = structlog.get_logger()

BATCH = 256

_PENDING = """
select c.id, s.tenant_id, c.ctx, c.text
from {d}.chunks c join {d}.sources s on s.id = c.source_id
where not exists (select 1 from {d}.chunk_vectors v where v.chunk_id = c.id)
  and s.status <> 'disabled' and (%(source)s::uuid is null or c.source_id = %(source)s)
  and s.tenant_id <> all(%(skip)s::uuid[])
order by s.tenant_id, c.id limit %(n)s"""

_STORE = """
insert into {d}.chunk_vectors (chunk_id, embedding)
select u.id, u.v::{ns}.vector from unnest(%s::uuid[], %s::text[]) u(id, v)
where exists (select 1 from {d}.chunks c where c.id = u.id)
on conflict do nothing"""


async def embed_pending(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    source_id: UUID | None = None,
    *,
    settings: DataSettings,
    meter: Meter | None = None,
) -> int:
    """Embed chunks that have no `chunk_vectors` row, one source's or every source's, and
    return how many were written. Nothing without an endpoint or the `vector` extension, and
    nothing for a model the rate table does not price when a meter bills it."""
    if not embed.configured(settings) or (ns := await embed.vector_schema(conn)) is None:
        return 0
    if meter is not None and not meter.priced(settings.embed_model):
        log.warning("data.embed_unpriced", model=settings.embed_model)
        return 0
    written = 0
    skip: list[UUID] = []
    while True:
        cur = await conn.execute(
            _PENDING.format(d=schemas.data).encode(),
            {"source": source_id, "skip": skip, "n": BATCH},
        )
        rows = await cur.fetchall()
        if not rows:
            break
        tenant: UUID = rows[0]["tenant_id"]
        batch = [r for r in rows if r["tenant_id"] == tenant]
        if meter is not None and not await meter.allowed(tenant):
            skip.append(tenant)
            continue
        try:
            vectors, tokens = await embed.embed(
                settings, [f"{r['ctx']}\n{r['text']}" if r["ctx"] else r["text"] for r in batch]
            )
        except embed.EmbedError as e:
            log.warning("data.embed_failed", error=str(e))
            break
        await conn.execute(
            _STORE.format(d=schemas.data, ns=ns).encode(),
            ([r["id"] for r in batch], [embed.vec(v) for v in vectors]),
        )
        written += len(batch)
        if meter is not None:
            try:
                await meter.record(
                    "data_embedding",
                    tenant_id=tenant,
                    platform_user_id=None,
                    model=settings.embed_model,
                    units=tokens,
                )
            except Exception:
                log.exception("data.embed_unbilled", tenant=str(tenant), tokens=tokens)
    if written:
        await embed.ensure_index(conn, schemas, settings.embed_dim, ns)
    return written
