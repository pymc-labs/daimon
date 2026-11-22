"""Viewers who let a page ask its agent as them. A consent holds for the version it
was given on; publishing a new version asks again."""

from __future__ import annotations

import uuid

from daimon.core._models import ArtifactAskConsent
from daimon.core.stores.domain import ArtifactRow
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def has_consent(session: AsyncSession, artifact: ArtifactRow, person_id: uuid.UUID) -> bool:
    """Given on the page's current version."""
    version = await session.scalar(
        select(ArtifactAskConsent.version).where(
            ArtifactAskConsent.artifact_id == artifact.id,
            ArtifactAskConsent.person_id == person_id,
        )
    )
    return version == artifact.current_version


async def record_consent(
    session: AsyncSession, artifact: ArtifactRow, person_id: uuid.UUID
) -> None:
    version = artifact.current_version
    await session.execute(
        insert(ArtifactAskConsent)
        .values(
            tenant_id=artifact.tenant_id,
            artifact_id=artifact.id,
            person_id=person_id,
            version=version,
        )
        .on_conflict_do_update(
            index_elements=["artifact_id", "person_id"], set_={"version": version}
        )
    )


async def delete_consent(
    session: AsyncSession, artifact_id: uuid.UUID, person_id: uuid.UUID
) -> None:
    await session.execute(
        delete(ArtifactAskConsent).where(
            ArtifactAskConsent.artifact_id == artifact_id,
            ArtifactAskConsent.person_id == person_id,
        )
    )
