"""Notices queued for an agent's next turn are taken once, per thread and agent, and go stale."""

from datetime import UTC, datetime, timedelta

from daimon.core.stores.agent_notices import record_agent_notices, take_agent_notices
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_take_returns_the_threads_notices_once_without_duplicates(
    db_session: AsyncSession,
) -> None:
    """A text queued twice is taken once, and a second take finds nothing."""
    tenant = await make_tenant(db_session)
    where = {"tenant_id": tenant.id, "platform": "discord", "thread_id": "t1", "agent_id": "a1"}
    await record_agent_notices(db_session, **where, texts=["a", "b", "a"])
    await record_agent_notices(db_session, **where, texts=["a"])
    await record_agent_notices(db_session, **{**where, "thread_id": "t2"}, texts=["other"])

    since = datetime.now(UTC) - timedelta(days=1)
    taken = await take_agent_notices(db_session, **where, since=since)
    assert sorted(taken) == ["a", "b"], "each text once, only this thread's"
    assert await take_agent_notices(db_session, **where, since=since) == [], "taken means gone"
    other = await take_agent_notices(db_session, **{**where, "thread_id": "t2"}, since=since)
    assert other == ["other"], "another thread keeps its own notices"


async def test_take_drops_notices_older_than_since(db_session: AsyncSession) -> None:
    """A stale notice is deleted without being returned."""
    tenant = await make_tenant(db_session)
    where = {"tenant_id": tenant.id, "platform": "slack", "thread_id": "t1", "agent_id": "a1"}
    await record_agent_notices(db_session, **where, texts=["old"])

    later = datetime.now(UTC) + timedelta(minutes=1)
    assert await take_agent_notices(db_session, **where, since=later) == [], "stale is dropped"
    early = later - timedelta(days=2)
    assert await take_agent_notices(db_session, **where, since=early) == [], "and deleted"


async def test_another_agents_notices_wait_for_it(db_session: AsyncSession) -> None:
    """After a hand-off, the new agent never hears of the old one's servers."""
    tenant = await make_tenant(db_session)
    where = {"tenant_id": tenant.id, "platform": "teams", "thread_id": "t1"}
    await record_agent_notices(db_session, **where, agent_id="a1", texts=["first's"])

    since = datetime.now(UTC) - timedelta(days=1)
    assert await take_agent_notices(db_session, **where, agent_id="a2", since=since) == []
    taken = await take_agent_notices(db_session, **where, agent_id="a1", since=since)
    assert taken == ["first's"], "the first agent still gets its own"


async def test_a_text_queued_again_counts_from_then(db_session: AsyncSession) -> None:
    """A repeat hand-over is news again, even when an older copy went stale unread."""
    tenant = await make_tenant(db_session)
    where = {"tenant_id": tenant.id, "platform": "slack", "thread_id": "t1", "agent_id": "a1"}
    await record_agent_notices(db_session, **where, texts=["handed over"])
    await db_session.execute(
        text(
            "UPDATE agent_notices SET created_at = now() - interval '2 days' WHERE tenant_id = :t"
        ),
        {"t": tenant.id},
    )
    await record_agent_notices(db_session, **where, texts=["handed over"])

    since = datetime.now(UTC) - timedelta(days=1)
    assert await take_agent_notices(db_session, **where, since=since) == ["handed over"]
