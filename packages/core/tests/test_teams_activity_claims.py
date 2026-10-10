"""Teams activity claims stay tenant-scoped and have bounded retention."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from daimon.core._models import TeamsActivityClaim, Tenant
from daimon.core.stores.teams_activity_claims import claim
from daimon.core.teams_activity_claim_sweep import sweep_old_teams_activity_claims
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_claim_is_atomic_and_scoped_to_tenant(
    db_session: AsyncSession,
) -> None:
    tenants = [uuid4(), uuid4()]
    db_session.add_all(
        [
            Tenant(id=tenant_id, platform="teams", external_id=str(tenant_id))
            for tenant_id in tenants
        ]
    )
    await db_session.flush()
    for tenant_id in tenants:
        assert await claim(
            db_session,
            tenant_id=tenant_id,
            conversation_id="same",
            activity_id="same",
            thread_id="same",
        )
        assert not await claim(
            db_session,
            tenant_id=tenant_id,
            conversation_id="same",
            activity_id="same",
            thread_id="same",
        )


async def test_sweep_prunes_only_old_claims(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = uuid4()
    db_session.add(Tenant(id=tenant_id, platform="teams", external_id=str(tenant_id)))
    await db_session.flush()
    now = datetime(2026, 10, 10, tzinfo=UTC)
    db_session.add_all(
        [
            TeamsActivityClaim(
                tenant_id=tenant_id,
                conversation_id="chat",
                activity_id=str(uuid4()),
                thread_id="chat",
                created_at=now - age,
            )
            for age in (timedelta(days=31), timedelta(days=29))
        ]
    )
    await db_session.commit()
    assert await sweep_old_teams_activity_claims(db_session_factory, now=now) == 1
    assert (
        await db_session.scalar(
            select(TeamsActivityClaim).where(
                TeamsActivityClaim.created_at >= now - timedelta(days=30)
            )
        )
    ) is not None
