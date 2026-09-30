"""Free-function store for seeded_skills.

Content fingerprint per (tenant_id, name) for skills shipped in
`defaults/skills/**`. This is the idempotence carrier MA does not provide —
see the `SeededSkill` model for why nothing on the provider side can hold it.

No try/except — exceptions propagate. None from `load_seeded_skill` means
'never seeded here', NEVER 'something broke'.
"""

from __future__ import annotations

import uuid

from daimon.core._models import SeededSkill
from daimon.core.stores.domain import SeededSkillRow
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def load_seeded_skill(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
) -> SeededSkillRow | None:
    orm = await session.get(SeededSkill, (tenant_id, name))
    if orm is None:
        return None
    return SeededSkillRow.model_validate(orm)


async def list_seeded_skill_names(session: AsyncSession, *, tenant_id: uuid.UUID) -> frozenset[str]:
    """Names of every skill `defaults apply` has seeded into this tenant."""
    result = await session.execute(
        select(SeededSkill.name).where(SeededSkill.tenant_id == tenant_id)
    )
    return frozenset(result.scalars())


async def record_seeded_skill(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    content_hash: str,
    anthropic_id: str,
) -> None:
    """Record the content just uploaded to MA under `anthropic_id`.

    Written AFTER the upload succeeds. The reverse order would let a failed
    upload leave a fingerprint claiming MA holds content it does not, and the
    skill would then never be retried.
    """
    stmt = pg_insert(SeededSkill).values(
        tenant_id=tenant_id,
        name=name,
        content_hash=content_hash,
        anthropic_id=anthropic_id,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[SeededSkill.tenant_id, SeededSkill.name],
            set_={"content_hash": content_hash, "anthropic_id": anthropic_id},
        )
    )
    await session.flush()
