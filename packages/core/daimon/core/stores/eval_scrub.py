"""Store for the scrub map: a keyed hash of each real value and the fake that replaced it."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from daimon.core._models import EvalScrubEntity
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def load_map(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[tuple[str, str], str]:
    rows = await session.execute(
        select(EvalScrubEntity.kind, EvalScrubEntity.key_hash, EvalScrubEntity.fake).where(
            EvalScrubEntity.tenant_id == tenant_id
        )
    )
    return {(kind, digest): fake for kind, digest, fake in rows}


async def save_map(
    session: AsyncSession, *, tenant_id: uuid.UUID, rows: Mapping[tuple[str, str], str]
) -> None:
    """Keep new entries; one already kept for the same value stays as it is."""
    if rows:
        await session.execute(
            insert(EvalScrubEntity)
            .values(
                [
                    {"tenant_id": tenant_id, "kind": k, "key_hash": h, "fake": f}
                    for (k, h), f in rows.items()
                ]
            )
            .on_conflict_do_nothing(
                index_elements=[
                    EvalScrubEntity.tenant_id,
                    EvalScrubEntity.kind,
                    EvalScrubEntity.key_hash,
                ]
            )
        )
