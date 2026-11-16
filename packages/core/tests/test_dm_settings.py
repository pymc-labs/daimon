"""Admin DM settings: a rule named by its position is read and written under one lock."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from daimon.core import dm_settings
from daimon.core.authz import AgentRef
from daimon.core.dm_access import DmAgent
from daimon.core.dm_settings import DmActor, DmRuleSpec
from daimon.core.stores import dm_access, web_audit
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

NOW = datetime(2026, 1, 1, tzinfo=UTC)
AGENTS = [DmAgent(name, f"ag_{name}", AgentRef.of(name)) for name in ("a", "b", "c")]


async def test_change_rule_when_a_removal_commits_first_then_changes_the_rule_now_there(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> None:
    """The change waits for the removal's lock, then reads the list it left, so its write
    and its audit row name the same rule."""
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant = await make_tenant(db_session)
    await db_session.commit()
    actor = DmActor(tenant.id, None, NOW)
    rules = [DmRuleSpec("group", "g1", "a"), DmRuleSpec("group", "g2", "b")]
    async with factory.begin() as session:
        await dm_settings.replace_rules(session, actor, AGENTS, rules)
    removed, release = asyncio.Event(), asyncio.Event()

    async def remove() -> None:
        async with factory.begin() as session:
            assert await dm_settings.remove_rule(session, actor, 0), "the first rule goes"
            removed.set()
            await release.wait()

    async def change() -> bool:
        async with factory.begin() as session:
            return await dm_settings.change_rule(
                session, actor, AGENTS, 0, agent="c", locked=False, to=None
            )

    first = asyncio.create_task(remove())
    await removed.wait()
    second = asyncio.create_task(change())
    await asyncio.sleep(0.2)
    assert not second.done(), "the change waits for the removal to commit"
    release.set()
    await first
    assert await second, "a rule is still at the position"
    async with factory() as session:
        stored = await dm_access.list_rules(session, tenant_id=tenant.id)
        rows = await web_audit.list_recent(session, tenant_id=tenant.id, action="Changed DM rule")
    assert [(r.subject_id, r.agent_name) for r in stored] == [("g2", "c")], stored
    assert [r.detail for r in rows] == ["Group g2 to c"], "the audit row names the rule changed"
