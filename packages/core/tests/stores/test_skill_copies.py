"""Store for carried skills: one size and one provider copy per source skill version."""

from __future__ import annotations

from daimon.core.stores.domain import SkillCopyRow, SkillSizeRow
from daimon.core.stores.skill_copies import (
    get_skill_copy,
    get_skill_sizes,
    put_skill_copy,
    put_skill_size,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_a_size_is_kept_per_version_and_the_first_measure_wins(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    row = SkillSizeRow(
        tenant_id=tenant.id,
        skill_id="skill",
        version="1",
        name="tidy",
        total_bytes=10,
        largest_file_bytes=10,
        file_count=1,
        text_only=True,
    )
    await put_skill_size(db_session, row)
    await put_skill_size(db_session, row.model_copy(update={"total_bytes": 99}))
    sizes = await get_skill_sizes(
        db_session, tenant_id=tenant.id, pins=(("skill", "1"), ("skill", "2"))
    )
    assert sizes == {("skill", "1"): row}, "only the measured version, as first written"


async def test_a_copy_is_kept_per_project_and_a_second_writer_reads_the_first(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    row = SkillCopyRow(
        tenant_id=tenant.id,
        provider="openai",
        account_scope_id="project",
        skill_id="skill",
        version="1",
        copy_id="copy-a",
        copy_version="1",
    )
    assert await put_skill_copy(db_session, row) == row
    assert await put_skill_copy(db_session, row.model_copy(update={"copy_id": "copy-b"})) == row
    other = await get_skill_copy(
        db_session,
        tenant_id=tenant.id,
        provider="openai",
        account_scope_id="other-project",
        skill_id="skill",
        version="1",
    )
    assert other is None, "another project uploads its own copy"
