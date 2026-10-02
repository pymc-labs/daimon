"""Concurrent submissions against the support ledger — real Postgres, two connections.

`record_escalation` counts then inserts. Without the per-person ledger lock two
transactions under READ COMMITTED both count the same rows and both insert, so
two fast submissions overspend the allowance, and two submissions on one
message record two requests. These tests run the writes on separate
connections at the same time and assert one winner.
"""

from __future__ import annotations

import asyncio

from daimon.core.stores import support_escalation as store
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


async def _count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        return int((await s.execute(text("SELECT count(*) FROM support_escalations"))).scalar_one())


async def _submit(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: object,
    *,
    message_id: str,
    allowance: int,
) -> store.EscalationOutcome:
    async with factory() as session, session.begin():
        outcome = await store.record_escalation_once(
            session,
            tenant_id=tenant_id,  # type: ignore[arg-type]
            account_id=None,
            platform="slack",
            platform_user_id="U_RACE",
            channel_id="C1",
            message_id=message_id,
            ma_session_id=None,
            note="help",
            allowance=allowance,
        )
        # Keep the transaction open so the other submitters are genuinely
        # concurrent with an uncommitted row.
        await asyncio.sleep(0.2)
        return outcome


async def test_a_double_submit_on_one_message_records_one_row(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> None:
    # Separate connections per session: the shared-connection test factory
    # would serialize the two transactions and hide the race.
    db_session_factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_RACE")
    await db_session.commit()

    results = await asyncio.gather(
        _submit(db_session_factory, tenant.id, message_id="1.1", allowance=5),
        _submit(db_session_factory, tenant.id, message_id="1.1", allowance=5),
    )

    assert sorted(r.status for r in results) == ["duplicate", "recorded"]
    assert await _count(db_session_factory) == 1, "a double click must spend one credit"


async def test_concurrent_requests_cannot_overspend_the_allowance(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> None:
    # Separate connections per session: the shared-connection test factory
    # would serialize the two transactions and hide the race.
    db_session_factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_RACE")
    await db_session.commit()

    results = await asyncio.gather(
        *(
            _submit(
                db_session_factory,
                tenant.id,
                message_id=f"2.{i}",
                allowance=1,
            )
            for i in range(3)
        )
    )

    assert sorted(r.status for r in results) == ["out_of_credits", "out_of_credits", "recorded"]
    assert await _count(db_session_factory) == 1
