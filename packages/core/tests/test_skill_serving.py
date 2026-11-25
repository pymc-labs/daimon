"""Serving the library's skills: a caller lists and reads only what their scopes allow."""

import uuid
from collections.abc import Collection

from daimon.core.skill_serving import list_served, read_served
from daimon.core.stores import workspace_skills as store
from daimon.core.workspace_skills import record_published
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class Allows:
    def __init__(self, *allowed: str) -> None:
        self.allowed = frozenset(allowed)

    async def visible(
        self, *, tenant_id: uuid.UUID, account_id: uuid.UUID | None, scopes: Collection[str]
    ) -> frozenset[str]:
        return self.allowed & frozenset(scopes)


async def test_a_caller_lists_and_reads_only_the_skills_their_scopes_allow(
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
            version="3",
            files={"SKILL.md": b"main", "notes/a.md": b"a"},
        )
    async with db_session_factory() as session, session.begin():
        await store.set_scope(session, tenant_id=tenant.id, skill_id="closed", scope="team")

    async def seen(scopes: Allows) -> tuple[list[str], list[str]]:
        async with db_session_factory() as session:
            listed = await list_served(session, scopes, tenant_id=tenant.id, account_id=None)
            read = [
                name
                for name in ("open", "closed")
                if await read_served(
                    session, scopes, tenant_id=tenant.id, account_id=None, name=name
                )
            ]
        return [s.name for s in listed], read

    assert await seen(Allows()) == (["open"], ["open"]), "a scoped skill hides from all"
    assert await seen(Allows("team")) == (["closed", "open"], ["open", "closed"])
    async with db_session_factory() as session:
        found = await read_served(
            session,
            Allows(),
            tenant_id=tenant.id,
            account_id=None,
            name="open",
            path="notes/a.md",
        )
    assert found is not None and found[1] == b"a" and found[0].version == "3"
    assert {f.path for f in found[0].files} == {"SKILL.md", "notes/a.md"}
