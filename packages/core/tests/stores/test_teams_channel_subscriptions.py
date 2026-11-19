"""Store tests for Graph subscriptions to Teams private and shared channels."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core.stores.teams_channel_subscriptions import (
    delete_teams_channel_subscriptions,
    get_teams_channel_subscription,
    insert_teams_channel_subscription,
    list_teams_channel_subscriptions,
    record_teams_channel_watermark,
    update_teams_channel_subscription,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

CHANNEL = "19:private@thread.tacv2"
AT = datetime(2026, 1, 1, tzinfo=UTC)


async def _subscribe(
    session: AsyncSession, tenant_id: uuid.UUID, channel_id: str, subscription_id: str
) -> bool:
    return await insert_teams_channel_subscription(
        session,
        tenant_id=tenant_id,
        channel_id=channel_id,
        team_id="19:team@thread.tacv2",
        group_id="g1",
        channel_type="private",
        channel_name="Secret",
        team_name="Team",
        bot_name="Daimon",
        service_url="https://smba.example/emea/",
        subscription_id=subscription_id,
        client_state_sha256=b"h" * 32,
        expires_at=AT + timedelta(days=3),
    )


async def _replace(session: AsyncSession, tenant_id: uuid.UUID, seen: str, new: str) -> bool:
    return await update_teams_channel_subscription(
        session,
        tenant_id=tenant_id,
        channel_id=CHANNEL,
        seen=seen,
        subscription_id=new,
        expires_at=AT + timedelta(days=6),
        client_state_sha256=b"n" * 32,
    )


async def test_a_replacement_keeps_the_watermark_and_rows_are_per_tenant(
    db_session: AsyncSession,
) -> None:
    tenant, other = await make_tenant(db_session), await make_tenant(db_session)
    await _subscribe(db_session, tenant.id, CHANNEL, "sub-1")
    await record_teams_channel_watermark(
        db_session, tenant_id=tenant.id, channel_id=CHANNEL, message_id="m1", at=AT
    )
    assert await _replace(db_session, tenant.id, "sub-1", "sub-2"), "the row read is replaced"
    row = await get_teams_channel_subscription(db_session, tenant_id=tenant.id, channel_id=CHANNEL)
    assert row is not None and row.subscription_id == "sub-2", "a recreation replaces the id"
    assert (row.last_message_id, row.last_message_at) == ("m1", AT), (
        "a recreated subscription catches up from where the old one stopped"
    )
    assert (
        await get_teams_channel_subscription(db_session, tenant_id=other.id, channel_id=CHANNEL)
        is None
    ), "rows are per tenant"


async def test_the_watermark_only_moves_forward(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _subscribe(db_session, tenant.id, CHANNEL, "sub-1")
    for message_id, at in (("m2", AT + timedelta(minutes=2)), ("m1", AT)):
        await record_teams_channel_watermark(
            db_session, tenant_id=tenant.id, channel_id=CHANNEL, message_id=message_id, at=at
        )
    row = await get_teams_channel_subscription(db_session, tenant_id=tenant.id, channel_id=CHANNEL)
    assert row is not None and row.last_message_id == "m2", "an older message never rewinds it"


async def test_list_filters_by_subscription_id_and_orders_by_expiry(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await _subscribe(db_session, tenant.id, CHANNEL, "sub-1")
    await _subscribe(db_session, tenant.id, "19:other@thread.tacv2", "sub-2")
    await update_teams_channel_subscription(
        db_session,
        tenant_id=tenant.id,
        channel_id=CHANNEL,
        seen="sub-1",
        subscription_id="sub-1",
        expires_at=AT + timedelta(days=5),
    )
    rows = await list_teams_channel_subscriptions(db_session, tenant_id=tenant.id)
    assert [row.subscription_id for row in rows] == ["sub-2", "sub-1"], "soonest expiry first"
    [only] = await list_teams_channel_subscriptions(
        db_session, tenant_id=tenant.id, subscription_ids=["sub-2", "unknown"]
    )
    assert only.channel_id == "19:other@thread.tacv2"


async def test_delete_by_team_returns_the_graph_ids_to_delete(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _subscribe(db_session, tenant.id, CHANNEL, "sub-1")
    await _subscribe(db_session, tenant.id, "19:other@thread.tacv2", "sub-2")
    removed = await delete_teams_channel_subscriptions(
        db_session, tenant_id=tenant.id, team_id="19:team@thread.tacv2"
    )
    assert sorted(removed) == ["sub-1", "sub-2"], "the caller deletes these on Graph too"
    assert await list_teams_channel_subscriptions(db_session, tenant_id=tenant.id) == []


async def test_writes_lose_to_a_subscription_another_process_recorded(
    db_session: AsyncSession,
) -> None:
    """Each write names the subscription it read, so a stale one changes nothing."""
    tenant = await make_tenant(db_session)
    assert await _subscribe(db_session, tenant.id, CHANNEL, "sub-1"), "the first is recorded"
    assert not await _subscribe(db_session, tenant.id, CHANNEL, "sub-2"), "a second is refused"
    assert await _replace(db_session, tenant.id, "sub-1", "sub-3"), "one process replaces it"
    assert not await _replace(db_session, tenant.id, "sub-1", "sub-4"), "the other read sub-1"
    removed = await delete_teams_channel_subscriptions(
        db_session, tenant_id=tenant.id, channel_id=CHANNEL, seen="sub-1"
    )
    row = await get_teams_channel_subscription(db_session, tenant_id=tenant.id, channel_id=CHANNEL)
    assert removed == [] and row is not None, "a stale drop keeps the row"
    assert (row.subscription_id, row.expires_at) == ("sub-3", AT + timedelta(days=6)), (
        "the winner's subscription stands"
    )
    removed = await delete_teams_channel_subscriptions(
        db_session, tenant_id=tenant.id, channel_id=CHANNEL, seen="sub-3"
    )
    assert removed == ["sub-3"], "a current drop deletes it"
