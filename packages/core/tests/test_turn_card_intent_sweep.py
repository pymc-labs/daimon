from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core._models import TurnCardIntent
from daimon.core.stores.turn_card_intents import create_turn_card_intent
from daimon.core.turn_card_intent_sweep import sweep_retired_turn_card_intents
from daimon.testing.factories import make_tenant
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_sweep_prunes_only_retired_intents_past_seven_days(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    rows = [
        await create_turn_card_intent(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id=f"thread-{index}",
            turn_token=uuid.uuid4(),
        )
        for index in range(3)
    ]
    now = datetime.now(UTC)
    await db_session.execute(
        update(TurnCardIntent)
        .where(TurnCardIntent.id == rows[0].id)
        .values(status="retired", updated_at=now - timedelta(days=8))
    )
    await db_session.execute(
        update(TurnCardIntent)
        .where(TurnCardIntent.id == rows[1].id)
        .values(status="retired", updated_at=now - timedelta(days=1))
    )
    await db_session.commit()

    deleted = await sweep_retired_turn_card_intents(db_session_factory, now=now)

    remaining = set((await db_session.execute(select(TurnCardIntent.id))).scalars().all())
    assert deleted == 1, "sweep should delete the retired intent older than seven days"
    assert rows[0].id not in remaining, "old retired intent should be pruned"
    assert rows[1].id in remaining, "recently retired intent should remain available"
    assert rows[2].id in remaining, "active intent should remain recoverable"
