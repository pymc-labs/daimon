"""Real database races and process ownership for startup replay."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from daimon.core.stores.discord_message_admissions import (
    claim_message,
    finish_messages,
    replay_channels,
)
from daimon.core.stores.thread_sessions import create_thread_session, update_watermark
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


async def test_two_startup_workers_admit_a_missed_message_once(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> None:
    db_session_factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant = await make_tenant(db_session)
    await db_session.commit()

    # Simulate two newly started processes with distinct live owner locks.
    async with db_session_factory() as owner_a, db_session_factory() as owner_b:
        await owner_a.execute(text("SELECT pg_advisory_lock(11)"))
        await owner_b.execute(text("SELECT pg_advisory_lock(22)"))
        try:

            async def worker(owner: int) -> bool:
                async with db_session_factory() as session:
                    result = await claim_message(
                        session,
                        tenant_id=tenant.id,
                        channel_id="1",
                        message_id="2",
                        owner_key=owner,
                    )
                    # Deliberately leave it pending, as a queued follow-up is.
                    # The other worker must not reclaim that admitted message.
                    await session.commit()
                    return result

            assert sorted(await asyncio.gather(worker(11), worker(22))) == [False, True]
            await finish_messages(db_session, tenant_id=tenant.id, message_ids=("2",))
            await db_session.commit()
        finally:
            await owner_a.execute(text("SELECT pg_advisory_unlock(11)"))
            await owner_b.execute(text("SELECT pg_advisory_unlock(22)"))
    assert not await worker(33), "a later replay must not start another turn"


async def test_pending_followup_is_reclaimed_only_after_its_process_exits(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> None:
    db_session_factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant = await make_tenant(db_session)
    await db_session.commit()
    async with db_session_factory() as owner:
        await owner.execute(text("SELECT pg_advisory_lock(421123)"))
        try:
            async with db_session_factory() as session:
                assert await claim_message(
                    session, tenant_id=tenant.id, channel_id="1", message_id="2", owner_key=421123
                )
                await session.commit()
            assert not await claim_message(
                db_session, tenant_id=tenant.id, channel_id="1", message_id="2", owner_key=421124
            )
            await db_session.commit()
        finally:
            await owner.execute(text("SELECT pg_advisory_unlock(421123)"))
    assert await claim_message(
        db_session, tenant_id=tenant.id, channel_id="1", message_id="2", owner_key=421124
    )
    await finish_messages(db_session, tenant_id=tenant.id, message_ids=("2",))
    await db_session.commit()
    assert not await claim_message(
        db_session, tenant_id=tenant.id, channel_id="1", message_id="2", owner_key=421125
    )


async def test_replay_channels_are_bounded_and_include_a_pending_message_before_latest_answer(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    row = await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="100",
        channel_id="parent",
        account_id=uuid4(),
        ma_session_id="sesn_test",
    )
    await update_watermark(db_session, id=row.id, watermark_message_id="300")
    assert await claim_message(
        db_session, tenant_id=tenant.id, channel_id="100", message_id="200", owner_key=11
    )
    channels = await replay_channels(
        db_session, tenant_id=tenant.id, cutoff=datetime.now(UTC) - timedelta(minutes=15), limit=25
    )
    assert ("100", "199") in channels, "an answer after a queued mention must not hide it"
    assert ("parent", None) in channels
    assert (
        len(
            await replay_channels(
                db_session,
                tenant_id=tenant.id,
                cutoff=datetime.now(UTC) - timedelta(minutes=15),
                limit=1,
            )
        )
        == 1
    )
    assert (
        await replay_channels(
            db_session,
            tenant_id=tenant.id,
            cutoff=datetime.now(UTC) + timedelta(minutes=1),
            limit=25,
        )
        == []
    )


async def test_replay_does_not_repeat_an_opening_turn_from_before_receipts(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="200",
        account_id=uuid4(),
        ma_session_id="sesn_old",
    )
    assert not await claim_message(
        db_session, tenant_id=tenant.id, channel_id="100", message_id="200", owner_key=11
    )
