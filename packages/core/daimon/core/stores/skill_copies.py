"""Store for `skill_sizes` and `skill_copies`: source skills carried to another provider.

Both are caches keyed by the source skill's concrete version, which never
changes its bytes: a row is written once and read by every later turn.
"""

from __future__ import annotations

import uuid

from daimon.core._models import SkillCopy, SkillSize
from daimon.core.stores.domain import SkillCopyRow, SkillSizeRow
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def get_skill_sizes(
    session: AsyncSession, *, tenant_id: uuid.UUID, pins: tuple[tuple[str, str], ...]
) -> dict[tuple[str, str], SkillSizeRow]:
    """The measured `(skill_id, version)` pins among `pins`."""
    if not pins:
        return {}
    rows = await session.scalars(
        select(SkillSize).where(
            SkillSize.tenant_id == tenant_id,
            SkillSize.skill_id.in_({skill for skill, _ in pins}),
        )
    )
    wanted = set(pins)
    return {
        (row.skill_id, row.version): SkillSizeRow.model_validate(row)
        for row in rows
        if (row.skill_id, row.version) in wanted
    }


async def put_skill_size(session: AsyncSession, row: SkillSizeRow) -> None:
    """Record one measurement; a concurrent writer measured the same bytes."""
    await session.execute(insert(SkillSize).values(**row.model_dump()).on_conflict_do_nothing())


async def get_skill_copy(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider: str,
    account_scope_id: str,
    skill_id: str,
    version: str,
) -> SkillCopyRow | None:
    row = await session.get(SkillCopy, (tenant_id, provider, account_scope_id, skill_id, version))
    return None if row is None else SkillCopyRow.model_validate(row)


async def put_skill_copy(session: AsyncSession, row: SkillCopyRow) -> SkillCopyRow:
    """Record one copy; the first writer wins and both read its row."""
    await session.execute(insert(SkillCopy).values(**row.model_dump()).on_conflict_do_nothing())
    stored = await get_skill_copy(
        session,
        tenant_id=row.tenant_id,
        provider=row.provider,
        account_scope_id=row.account_scope_id,
        skill_id=row.skill_id,
        version=row.version,
    )
    if stored is None:
        raise RuntimeError("skill copy row missing after insert")
    return stored
