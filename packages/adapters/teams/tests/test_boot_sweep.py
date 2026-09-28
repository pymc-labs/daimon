"""Boot sweep: cards a restart cut off are marked interrupted, never resumed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.teams import card
from daimon.adapters.teams.boot_sweep import retire_orphaned_turns
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.thread_sessions import get_thread_session_by_id, mark_turn_active
from daimon.core.stores.turn_card_intents import (
    create_turn_card_intent,
    list_recoverable_turn_card_intents,
    record_turn_card_message,
)
from daimon.testing.factories import make_thread_session
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import CONVERSATION_ID, ENTRA_TENANT_ID, THREAD_ID, FakeSender

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


async def _intent(session: AsyncSession, *, thread_id: str, message_id: str | None) -> uuid.UUID:
    intent = await create_turn_card_intent(
        session,
        tenant_id=TENANT,
        platform="teams",
        thread_id=thread_id,
        turn_token=uuid.uuid4(),
        channel_id=thread_id,
    )
    if message_id is not None:
        await record_turn_card_message(session, id=intent.id, message_id=message_id)
    await session.commit()
    return intent.id


@pytest.mark.usefixtures("provisioned_tenant")
async def test_markers_and_intents_are_interrupted_cleared_and_retired(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await make_thread_session(db_session, platform="teams", thread_id=CONVERSATION_ID)
    await mark_turn_active(
        db_session,
        id=row.id,
        active_turn_message_id="m-dm",
        active_turn_channel_id=CONVERSATION_ID,
        now=datetime.now(UTC),
    )
    await db_session.commit()
    await _intent(db_session, thread_id=CONVERSATION_ID, message_id="m-dm")
    await _intent(db_session, thread_id=THREAD_ID, message_id="m-channel")
    await _intent(db_session, thread_id=THREAD_ID, message_id=None)
    sender = FakeSender(fail_on={1})

    with patch(
        "daimon.adapters.teams.boot_sweep.interrupt_orphaned_session", new_callable=AsyncMock
    ) as interrupt:
        await retire_orphaned_turns(
            anthropic=AsyncMock(),
            sessionmaker=db_session_factory,
            sender=sender,
            now=datetime.now(UTC),
        )

    edits = [(conversation, activity.id) for conversation, activity, _ in sender.sent]
    assert edits == [(CONVERSATION_ID, "m-dm"), (THREAD_ID, "m-channel")], "one edit per card"
    assert all(card.INTERRUPTED_NOTICE in a.model_dump_json() for a in sender.activities)
    interrupt.assert_awaited_once()
    assert interrupt.await_args is not None
    assert interrupt.await_args.kwargs["session_id"] == row.ma_session_id

    async with db_session_factory() as session:
        cleared = await get_thread_session_by_id(session, id=row.id)
        open_intents = await list_recoverable_turn_card_intents(session, platform="teams")
    assert cleared is not None and cleared.active_turn_message_id is None, "marker cleared"
    assert open_intents == [], "every intent retires, failed edits and unposted cards too"
