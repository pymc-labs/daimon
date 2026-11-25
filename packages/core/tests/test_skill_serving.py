"""Serving a library skill reads its latest version's files from the local copy."""

from daimon.core.skill_serving import read_served
from daimon.core.stores import workspace_skills as store
from daimon.core.workspace_skills import record_published
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_a_read_serves_a_listed_skills_latest_files_only(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    await record_published(
        db_session_factory,
        tenant_id=tenant.id,
        skill_id="s1",
        name="notes",
        version="3",
        files={"SKILL.md": b"main", "notes/a.md": b"a"},
    )
    async with db_session_factory() as session:
        rows = await store.list_skills(session, tenant_id=tenant.id)
        found = await read_served(session, rows, name="notes", path="notes/a.md")
        assert await read_served(session, rows, name="notes", path="none.md") is None
        assert await read_served(session, [], name="notes") is None, "only listed skills"
    assert found is not None and found[1] == b"a" and found[0].version == "3"
    assert {f.path for f in found[0].files} == {"SKILL.md", "notes/a.md"}
