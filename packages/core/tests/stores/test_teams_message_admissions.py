"""Teams admissions against a real database: once per message, across processes."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from secrets import randbits

from daimon.core._models import TeamsMessageAdmission
from daimon.core.stores.teams_message_admissions import (
    claim_teams_message,
    count_teams_messages_for_user,
    delete_teams_messages_for_user,
    finish_teams_messages,
    mark_restart_notice,
    prune_teams_messages,
    reclaim_teams_messages,
)
from daimon.testing.factories import make_tenant
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

CHANNEL = "19:abc@thread.tacv2"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def _claim(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    owner: int,
    message_id: str = "1",
    *,
    payload: bool = True,
    user_id: str | None = None,
) -> bool:
    return await claim_teams_message(
        session,
        tenant_id=tenant_id,
        channel_id=CHANNEL,
        message_id=message_id,
        conversation_id=f"{CHANNEL};messageid={message_id}",
        owner_key=owner,
        sent_at=NOW,
        user_id=user_id,
        message={"text": "hi"} if payload else None,
    )


async def _finish(session: AsyncSession, tenant_id: uuid.UUID, owner: int, *ids: str) -> bool:
    return await finish_teams_messages(
        session, tenant_id=tenant_id, channel_id=CHANNEL, message_ids=ids or ("1",), owner_key=owner
    )


@asynccontextmanager
async def _alive(engine: AsyncEngine, owner: int) -> AsyncIterator[None]:
    """A running process: it holds its owner lock on its own connection."""
    async with async_sessionmaker(engine)() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": owner})
        try:
            yield
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": owner})


async def _age(session: AsyncSession, by: timedelta) -> None:
    await session.execute(
        update(TeamsMessageAdmission).values(created_at=TeamsMessageAdmission.created_at - by)
    )
    await session.commit()


async def test_two_processes_admit_a_message_once_and_redeliveries_never(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> None:
    """A Bot Framework and a Graph delivery of one message race: one wins, then none."""
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant = await make_tenant(db_session)
    await db_session.commit()
    a, b = randbits(30), randbits(30) + 2**31

    async def worker(owner: int) -> bool:
        async with factory.begin() as session:
            return await _claim(session, tenant.id, owner)

    async with _alive(db_nullpool_engine, a), _alive(db_nullpool_engine, b):
        won = await asyncio.gather(worker(a), worker(b))
        assert sorted(won) == [False, True], "exactly one process admits the message"
        assert not await worker(a) and not await worker(b), "a redelivery is refused"
    winner = a if won[0] else b
    async with factory.begin() as session:
        assert await _finish(session, tenant.id, winner), "the winner finishes it"
    assert not await worker(randbits(30)), "a handled message is never admitted again"


async def test_an_unhandled_message_is_reclaimed_only_once_its_process_exits(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    old, new = randbits(30), randbits(30) + 2**31
    window = {
        "replay_after": datetime.now(UTC) - timedelta(minutes=10),
        "horizon": datetime.now(UTC) - timedelta(days=1),
    }

    async with _alive(db_nullpool_engine, old):
        assert await _claim(db_session, tenant.id, old)
        await db_session.commit()
        live = await reclaim_teams_messages(
            db_session, tenant_id=tenant.id, owner_key=new, **window
        )
        assert live.replay == [] and live.stale == [], "a live owner keeps its message"
        assert not await _claim(db_session, tenant.id, new), "nor can a redelivery take it"
        await db_session.commit()

    taken = await reclaim_teams_messages(db_session, tenant_id=tenant.id, owner_key=new, **window)
    await db_session.commit()
    assert [row.message for row in taken.replay] == [{"text": "hi"}], "the payload replays"
    async with _alive(db_nullpool_engine, new):
        again = await reclaim_teams_messages(
            db_session, tenant_id=tenant.id, owner_key=randbits(30), **window
        )
    assert again.replay == [], "another boot gets nothing while the reclaimer runs"
    assert not await _finish(db_session, tenant.id, old), "the exited owner lost its fence"
    assert await _finish(db_session, tenant.id, new), "the reclaimer holds it now"


async def test_a_stale_message_is_handled_before_anyone_is_told(
    db_session: AsyncSession,
) -> None:
    """A lost 'send it again' is never repeated: the row is closed as it is taken."""
    tenant = await make_tenant(db_session)
    dead = randbits(30)
    assert await _claim(db_session, tenant.id, dead)
    await _age(db_session, timedelta(minutes=30))
    window = {
        "replay_after": datetime.now(UTC) - timedelta(minutes=10),
        "horizon": datetime.now(UTC) - timedelta(days=1),
    }

    first = await reclaim_teams_messages(
        db_session, tenant_id=tenant.id, owner_key=randbits(30) + 2**31, **window
    )
    second = await reclaim_teams_messages(
        db_session, tenant_id=tenant.id, owner_key=randbits(30) + 2**31, **window
    )

    assert [row.message_id for row in first.stale] == ["1"], "the stale row is taken once"
    assert first.replay == [] and second.stale == [], "and never again, nor replayed"


async def test_the_restart_notice_is_marked_once(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    owner = randbits(30)
    assert await _claim(db_session, tenant.id, owner, "1")
    assert await _claim(db_session, tenant.id, owner, "2")

    async def mark(*ids: str) -> bool:
        return await mark_restart_notice(
            db_session, tenant_id=tenant.id, channel_id=CHANNEL, message_ids=ids, owner_key=owner
        )

    assert await mark("1", "2"), "the first drain owes the notice"
    assert not await mark("1", "2"), "a second drain does not"
    assert await _finish(db_session, tenant.id, owner, "1", "2")
    assert await _claim(db_session, tenant.id, owner, "3")
    assert await mark("3"), "a later message owes its own"


async def test_a_message_answered_at_once_keeps_no_payload(db_session: AsyncSession) -> None:
    """Participation and refusals store a receipt only: nothing for a restart to replay."""
    tenant = await make_tenant(db_session)
    assert await _claim(db_session, tenant.id, randbits(30), payload=False)
    taken = await reclaim_teams_messages(
        db_session,
        tenant_id=tenant.id,
        owner_key=randbits(30) + 2**31,
        replay_after=NOW,
        horizon=NOW - timedelta(days=1),
    )
    assert taken.replay == [] and taken.stale == [], "a receipt is already handled"


async def test_prune_keeps_recent_rows_and_a_live_owners(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    me, live, dead = randbits(30), randbits(30) + 2**31, randbits(30) + 2**32
    async with _alive(db_nullpool_engine, live):
        assert await _claim(db_session, tenant.id, me, "handled", payload=False)
        assert await _claim(db_session, tenant.id, live, "live")
        assert await _claim(db_session, tenant.id, dead, "dead")
        await _age(db_session, timedelta(days=2))
        assert await _claim(db_session, tenant.id, me, "recent", payload=False)
        await db_session.commit()

        pruned = await prune_teams_messages(
            db_session,
            tenant_id=tenant.id,
            before=datetime.now(UTC) - timedelta(days=1),
            owner_key=me,
        )
        await db_session.commit()

        assert pruned == 2, "the old receipt and the dead owner's row go"
        assert not await _claim(db_session, tenant.id, me, "recent"), "a recent receipt stays"
        assert not await _claim(db_session, tenant.id, me, "live"), "a live owner's row stays"


async def test_a_persons_rows_are_counted_and_purged(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    owner = randbits(30)
    assert await _claim(db_session, tenant.id, owner, "1", user_id="u-1")
    assert await _claim(db_session, tenant.id, owner, "2", user_id="u-2")

    counted = await count_teams_messages_for_user(db_session, tenant_id=tenant.id, user_id="u-1")
    deleted = await delete_teams_messages_for_user(db_session, tenant_id=tenant.id, user_id="u-1")

    assert (counted, deleted) == (1, 1), "only that person's row, counted as deleted"
    assert await count_teams_messages_for_user(db_session, tenant_id=tenant.id, user_id="u-1") == 0


async def test_a_message_for_a_tenant_not_yet_provisioned_is_let_through(
    db_session: AsyncSession,
) -> None:
    """No receipt can be kept; admission refuses the message instead."""
    assert await _claim(db_session, uuid.uuid4(), randbits(30)), "not refused as a duplicate"
