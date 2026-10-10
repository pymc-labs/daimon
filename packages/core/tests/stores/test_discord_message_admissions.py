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
    # A later message can be claimed first by another gateway worker. Claim
    # order must not hide the earlier pending input behind the answer watermark.
    assert await claim_message(
        db_session, tenant_id=tenant.id, channel_id="100", message_id="400", owner_key=11
    )
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


async def test_new_ledger_channel_does_not_use_an_answer_watermark_to_hide_missed_inputs(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    row = await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="100",
        account_id=uuid4(),
        ma_session_id="sesn_long",
    )
    assert await claim_message(
        db_session, tenant_id=tenant.id, channel_id="100", message_id="200", owner_key=11
    )
    await finish_messages(db_session, tenant_id=tenant.id, message_ids=("200",))
    await db_session.execute(
        text("UPDATE discord_message_admissions SET created_at = :old WHERE tenant_id = :tenant"),
        {"old": datetime.now(UTC) - timedelta(minutes=20), "tenant": tenant.id},
    )
    # A long turn's final answer can arrive after a missed, unreceipted mention
    # during draining. Its answer ID must not hide that input on the next boot.
    await update_watermark(db_session, id=row.id, watermark_message_id="400")
    channels = await replay_channels(
        db_session, tenant_id=tenant.id, cutoff=datetime.now(UTC) - timedelta(minutes=15), limit=25
    )
    assert ("100", None) in channels, "ledger-era channels scan the whole bounded time window"


async def test_retention_preserves_channel_boundary_recent_receipts_and_live_pending_inputs(
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
) -> None:
    from daimon.core._models import DiscordMessageAdmission
    from daimon.core.stores.discord_message_admissions import prune_old_messages
    from sqlalchemy import select, update

    tenant = await make_tenant(db_session)
    await db_session.commit()
    now = datetime.now(UTC)
    async with db_nullpool_engine.connect() as owner:
        await owner.execute(text("SELECT pg_advisory_lock(718213)"))
        try:
            for message_id, age, handled, owner_key in [
                ("1", 121, True, 1),  # channel anchor survives all sweeps
                ("2", 120, True, 1),
                ("3", 119, False, 1),  # dead pending input beyond the horizon
                ("4", 118, False, 718213),  # live long-running queued input
                ("5", 1, True, 1),
            ]:
                assert await claim_message(
                    db_session,
                    tenant_id=tenant.id,
                    channel_id="100",
                    message_id=message_id,
                    owner_key=owner_key,
                )
                await db_session.execute(
                    update(DiscordMessageAdmission)
                    .where(
                        DiscordMessageAdmission.tenant_id == tenant.id,
                        DiscordMessageAdmission.message_id == message_id,
                    )
                    .values(created_at=now - timedelta(minutes=age), handled=handled)
                )
            await db_session.commit()
            assert (
                await prune_old_messages(
                    db_session,
                    tenant_id=tenant.id,
                    cutoff=now - timedelta(minutes=60),
                )
                == 2
            )
            await db_session.commit()
            remaining = await db_session.scalars(
                select(DiscordMessageAdmission.message_id).where(
                    DiscordMessageAdmission.tenant_id == tenant.id,
                )
            )
            assert set(remaining) == {"1", "4", "5"}
            assert await replay_channels(
                db_session,
                tenant_id=tenant.id,
                cutoff=now - timedelta(minutes=15),
                limit=25,
            ) == [("100", None)], "pruning must not restore an unsafe legacy answer boundary"
            assert not await claim_message(
                db_session,
                tenant_id=tenant.id,
                channel_id="100",
                message_id="5",
                owner_key=2,
            )
        finally:
            await owner.execute(text("SELECT pg_advisory_unlock(718213)"))
