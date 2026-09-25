from __future__ import annotations

import asyncio
import uuid

import pytest
from daimon.core.stores.turn_card_intents import (
    TurnCardIntentConflictError,
    create_turn_card_intent,
    list_recoverable_turn_card_intents,
    record_turn_card_message,
    retire_turn_card_intent,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


async def test_turn_card_intent_is_boot_visible_before_and_after_message_recording(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    intent = await create_turn_card_intent(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        thread_id="thread-ts",
        turn_token=uuid.uuid4(),
        channel_id="channel-1",
    )

    prepared = await list_recoverable_turn_card_intents(db_session, platform="slack")
    assert len(prepared) == 1, "boot listing must include a prepared intent with no message ID"
    assert prepared[0].id == intent.id, "boot listing must return the newly prepared intent"
    assert prepared[0].status == "prepared", "unpersisted platform response remains distinguishable"
    assert prepared[0].message_id is None, "prepared intents must not invent a platform message ID"
    assert await list_recoverable_turn_card_intents(db_session, platform="discord") == [], (
        "boot listing must be scoped to the adapter platform"
    )

    recorded = await record_turn_card_message(db_session, id=intent.id, message_id="message-1")
    assert recorded, "the first returned message ID should transition the intent to posted"
    posted = await list_recoverable_turn_card_intents(db_session, platform="slack")
    assert posted[0].status == "posted", "recorded platform messages must remain recoverable"
    assert posted[0].message_id == "message-1", "boot listing must preserve the platform message ID"


async def test_turn_card_intent_message_update_and_retirement_are_conditional(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    intent = await create_turn_card_intent(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-1",
        turn_token=uuid.uuid4(),
    )

    assert await record_turn_card_message(db_session, id=intent.id, message_id="m1"), (
        "a prepared intent should accept its platform message ID"
    )
    assert await record_turn_card_message(db_session, id=intent.id, message_id="m1"), (
        "recording the same message ID must be idempotent"
    )
    assert not await record_turn_card_message(db_session, id=intent.id, message_id="m2"), (
        "a second message ID must not overwrite the recorded post"
    )
    assert not await retire_turn_card_intent(
        db_session, id=intent.id, expected_message_id="stale-message"
    ), "retirement must preserve an intent when its message ID changed"
    assert await retire_turn_card_intent(db_session, id=intent.id, expected_message_id="m1"), (
        "retirement should succeed for the message ID the caller observed"
    )
    assert not await retire_turn_card_intent(db_session, id=intent.id, expected_message_id="m1"), (
        "retiring an already retired intent must be a no-op"
    )
    assert await list_recoverable_turn_card_intents(db_session, platform="discord") == [], (
        "retired intents must leave the boot recovery listing"
    )


async def test_duplicate_concurrent_turn_token_returns_one_persisted_intent(
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    token = uuid.uuid4()
    ready = asyncio.Barrier(2)

    async def create_concurrently() -> uuid.UUID:
        await ready.wait()
        async with factory() as session, session.begin():
            row = await create_turn_card_intent(
                session,
                tenant_id=tenant.id,
                platform="discord",
                thread_id="thread-shared",
                turn_token=token,
            )
            return row.id

    first_id, second_id = await asyncio.gather(create_concurrently(), create_concurrently())

    assert first_id == second_id, "concurrent retries must resolve to the same unique intent row"
    rows = await list_recoverable_turn_card_intents(db_session, platform="discord")
    assert len(rows) == 1, "the database uniqueness key must prevent duplicate intents"


async def test_boot_listing_returns_all_active_thread_intents_and_retirement_is_row_scoped(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    first = await create_turn_card_intent(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-overlap",
        turn_token=uuid.uuid4(),
    )
    second = await create_turn_card_intent(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-overlap",
        turn_token=uuid.uuid4(),
    )
    assert first.id != second.id, "distinct turn tokens must create independent intent rows"
    assert await record_turn_card_message(db_session, id=first.id, message_id="m1"), (
        "the first overlapping intent must keep its own message ID"
    )
    assert await record_turn_card_message(db_session, id=second.id, message_id="m2"), (
        "the second overlapping intent must keep its own message ID"
    )

    rows = await list_recoverable_turn_card_intents(db_session, platform="discord")
    assert {row.id for row in rows} == {first.id, second.id}, (
        "boot listing must return every active intent, including multiple turns in one thread"
    )
    assert not await retire_turn_card_intent(db_session, id=first.id, expected_message_id="m2"), (
        "a stale recovery snapshot must not match another turn's message ID"
    )
    assert await retire_turn_card_intent(db_session, id=first.id, expected_message_id="m1"), (
        "a recovery snapshot may retire only its own still-matching row"
    )

    remaining = await list_recoverable_turn_card_intents(db_session, platform="discord")
    assert len(remaining) == 1 and remaining[0].id == second.id, (
        "retiring one turn must leave the newer active intent discoverable"
    )


async def test_turn_card_intent_creation_rolls_back_with_callers_transaction(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await db_session.commit()
    await create_turn_card_intent(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        thread_id="thread-rollback",
        turn_token=uuid.uuid4(),
        channel_id="channel-rollback",
    )
    await db_session.rollback()

    rows = await list_recoverable_turn_card_intents(db_session, platform="slack")
    assert rows == [], "an uncommitted intent must not survive a transaction rollback"


@pytest.mark.parametrize(
    ("platform", "thread_id", "channel_id"),
    [
        ("slack", "thread-1", "channel-2"),
        ("slack", "thread-2", "channel-1"),
        ("discord", "thread-1", None),
    ],
)
async def test_turn_card_intent_rejects_reusing_token_for_another_platform_address(
    db_session: AsyncSession,
    platform: str,
    thread_id: str,
    channel_id: str | None,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    token = uuid.uuid4()
    await create_turn_card_intent(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        thread_id="thread-1",
        turn_token=token,
        channel_id="channel-1",
    )

    with pytest.raises(TurnCardIntentConflictError):
        await create_turn_card_intent(
            db_session,
            tenant_id=tenant.id,
            platform=platform,
            thread_id=thread_id,
            turn_token=token,
            channel_id=channel_id,
        )
