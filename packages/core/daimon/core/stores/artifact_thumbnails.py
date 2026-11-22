"""Version thumbnails of artifact pages: one small PNG per version, set by a manager's
preview and replaced by the next capture."""

from __future__ import annotations

import uuid

from daimon.core._models import ArtifactThumbnail
from daimon.core.stores.domain import ArtifactRow
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def get_thumbnail(
    session: AsyncSession, artifact_id: uuid.UUID, version: int
) -> bytes | None:
    return await session.scalar(
        select(ArtifactThumbnail.png).where(
            ArtifactThumbnail.artifact_id == artifact_id, ArtifactThumbnail.version == version
        )
    )


async def has_thumbnail(session: AsyncSession, artifact_id: uuid.UUID, version: int) -> bool:
    found = await session.scalar(
        select(ArtifactThumbnail.version).where(
            ArtifactThumbnail.artifact_id == artifact_id, ArtifactThumbnail.version == version
        )
    )
    return found is not None


async def upsert_thumbnail(
    session: AsyncSession, artifact: ArtifactRow, version: int, png: bytes
) -> None:
    """The version must exist: its foreign key refuses one that does not."""
    values = {"tenant_id": artifact.tenant_id, "artifact_id": artifact.id, "version": version}
    await session.execute(
        insert(ArtifactThumbnail)
        .values(**values, png=png)
        .on_conflict_do_update(index_elements=["artifact_id", "version"], set_={"png": png})
    )
