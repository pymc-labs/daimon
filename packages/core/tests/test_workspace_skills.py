"""Kept skill versions: recording is repeatable and a skill's scope decides who lists it."""

import uuid
from collections.abc import Collection

from daimon.core.stores import workspace_skills as store
from daimon.core.workspace_skills import (
    NoScopes,
    files_digest,
    list_visible_skills,
    record_published,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class Allows:
    def __init__(self, *allowed: str) -> None:
        self.allowed = frozenset(allowed)

    async def visible(
        self, *, tenant_id: uuid.UUID, account_id: uuid.UUID | None, scopes: Collection[str]
    ) -> frozenset[str]:
        return self.allowed & frozenset(scopes)


async def test_recording_a_version_twice_keeps_one_copy(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    files = {"SKILL.md": b"one"}
    for _ in range(2):
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
    assert [f.path for f in kept] == ["SKILL.md"]
    assert files_digest(files) != files_digest({"SKILL.md": b"two"})


async def test_a_scoped_skill_is_listed_only_for_callers_the_checker_allows(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    for skill_id in ("open", "closed"):
        await record_published(
            db_session_factory,
            tenant_id=tenant.id,
            skill_id=skill_id,
            name=skill_id,
            version="1",
            files={"SKILL.md": b"x"},
        )
    async with db_session_factory() as session, session.begin():
        await store.set_scope(session, tenant_id=tenant.id, skill_id="closed", scope="team")

    async def listed(scopes: Allows | NoScopes) -> list[str]:
        async with db_session_factory() as session:
            rows = await list_visible_skills(session, scopes, tenant_id=tenant.id, account_id=None)
        return sorted(row.skill_id for row in rows)

    assert await listed(NoScopes()) == ["open"]
    assert await listed(Allows("team")) == ["closed", "open"]
