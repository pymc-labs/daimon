"""The seam between sync and embeddings.

A run leaves a chunk without a vector; the embedder fills the gap afterwards, outside any
write transaction, so a sync never waits on a model. The sync loop calls `embed_pending`
after each run and once a day for gaps, only when an embedding endpoint is configured.
"""

from __future__ import annotations

from uuid import UUID

from daimon.data.schema import Schemas
from psycopg import AsyncConnection
from psycopg.rows import DictRow


async def embed_pending(
    conn: AsyncConnection[DictRow], schemas: Schemas, source_id: UUID | None = None
) -> int:
    """Embed chunks that have no `chunk_vectors` row, one source's or every source's, and
    return how many were written. Fills in with the embeddings milestone: until then no
    vector is written and no model is called."""
    return 0
