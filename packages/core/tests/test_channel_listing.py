"""Channel listing: one round stores what the platform lists; rounds repeat until stopped."""

from __future__ import annotations

from daimon.core.channel_listing import run_channel_listing, store_listing
from daimon.core.stores.platform_names import ChannelSighting, list_channel_names
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_a_listing_stores_kind_parent_and_archived(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T1")
    await db_session.commit()
    stored = await store_listing(
        db_session_factory,
        tenant_id=tenant.id,
        platform="slack",
        channels={
            "C1": ChannelSighting("general", kind="public", archived=False),
            "C2": ChannelSighting("old-launch", kind="private", archived=True),
        },
    )
    rows = await list_channel_names(db_session, tenant_id=tenant.id, platform="slack")
    assert stored == 2
    assert sorted((r.channel_id, r.kind, r.archived) for r in rows) == [
        ("C1", "public", False),
        ("C2", "private", True),
    ]
    assert all(r.last_seen_at is not None for r in rows)
    assert (
        await store_listing(db_session_factory, tenant_id=tenant.id, platform="slack", channels={})
        == 0
    )


async def test_rounds_repeat_past_a_failure_until_told_to_stop() -> None:
    rounds: list[int] = []

    async def list_once() -> int:
        rounds.append(len(rounds))
        if len(rounds) == 1:
            raise RuntimeError("platform unavailable")
        return 3

    await run_channel_listing(
        list_once,
        platform="slack",
        should_stop=lambda: len(rounds) >= 3,
        interval_s=0,
        first_round_s=0,
    )
    assert rounds == [0, 1, 2]
