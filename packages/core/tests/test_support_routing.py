"""Support DM tiers: the channel's admins, then the server admins."""

from __future__ import annotations

from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.support_routing import support_recipient_tiers
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_live_server_admins_replace_the_stored_ones(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Teams passes its admin team's owners as the second tier, the requester left out."""
    tenant = await make_tenant(db_session, platform="teams", workspace_id="entra-tenant")
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="teams",
        channel_id="19:chan@thread.tacv2",
        role_ids=[],
        user_ids=["chan-admin"],
        actor_account_id=None,
    )
    await db_session.commit()

    async def owners() -> tuple[str, ...]:
        return ("requester", "owner-1")

    tiers = await support_recipient_tiers(
        db_session_factory,
        tenant_id=tenant.id,
        platform="teams",
        channel_id="19:chan@thread.tacv2",
        requester_id="requester",
        server_admins=owners,
    )
    assert tiers == (("chan-admin",), ("owner-1",)), "channel admins, then the live owners"
