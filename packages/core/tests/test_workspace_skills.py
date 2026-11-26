"""Kept skill versions: kept where serving is On (the default), repeatable, never back, scope kept."""

from daimon.core.capability_tools import LIBRARY_SKILLS
from daimon.core.stores import workspace_skills as store
from daimon.core.workspace_skills import files_digest, may_read, record_published
from daimon.testing.factories import make_tenant, turn_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_a_version_is_kept_once_and_only_where_serving_is_on(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await turn_on(db_session, tenant_id=tenant.id, key=LIBRARY_SKILLS, value="off")
    await db_session.commit()
    files = {"SKILL.md": b"one"}

    async def record() -> list[str]:
        await record_published(
            db_session_factory,
            tenant_id=tenant.id,
            skill_id="s1",
            name="notes",
            version="1",
            files=files,
        )
        async with db_session_factory() as session:
            kept = await store.list_files(session, tenant_id=tenant.id, skill_id="s1", version="1")
        return [f.path for f in kept]

    assert await record() == [], "nothing is kept while library_skills is Off"
    async with db_session_factory.begin() as session:
        await turn_on(session, tenant_id=tenant.id, key=LIBRARY_SKILLS)
    assert await record() == await record() == ["SKILL.md"]
    assert files_digest(files) != files_digest({"SKILL.md": b"two"})


async def test_a_skill_keeps_its_scope_and_never_goes_back_a_version(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await turn_on(db_session, tenant_id=tenant.id, key=LIBRARY_SKILLS)
    await db_session.commit()
    for version, scope in (("9", "team"), ("10", None), ("2", None)):
        await record_published(
            db_session_factory,
            tenant_id=tenant.id,
            skill_id="s1",
            name="notes",
            version=version,
            files={"SKILL.md": version.encode()},
            scope=scope,
        )
    async with db_session_factory() as session:
        row = await store.get_skill(session, tenant_id=tenant.id, skill_id="s1")
    assert row is not None and (row.current_version, row.scope) == ("10", "team")
    assert may_read(row, ["team"]) and not may_read(row, [])
