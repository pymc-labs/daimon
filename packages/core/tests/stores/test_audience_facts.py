"""Room facts per server: one complete set, or unknown."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core.audience_facts import UNKNOWN_FACTS, RoomFacts
from daimon.core.stores import audience_facts as store
from daimon.core.stores.audience_facts import StoredRoom
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 5, 1, tzinfo=UTC)
LATER = NOW + timedelta(seconds=30)
OPEN = RoomFacts(False, False, 0, 0)
CONTAINED = RoomFacts(True, False, 2, 0)
NARROW = RoomFacts(False, True, 0, 4)


async def _publish(session: AsyncSession, tenant_id: uuid.UUID, *, at: datetime = NOW) -> int:
    return await store.publish_facts(
        session,
        tenant_id=tenant_id,
        rooms={
            "1": StoredRoom(OPEN),
            "2": StoredRoom(CONTAINED),
            "3": StoredRoom(NARROW),
            "9": StoredRoom(UNKNOWN_FACTS),
        },
        now=at,
        fresh_until=at + timedelta(seconds=180),
    )


async def _read(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    channel: str,
    at: datetime = LATER,
    parent_id: str | None = None,
    public_thread: bool = False,
) -> RoomFacts:
    return await store.read_channel_facts(
        session,
        tenant_id=tenant_id,
        channel_id=channel,
        now=at,
        parent_id=parent_id,
        public_thread=public_thread,
    )


async def test_a_published_set_is_read_per_room_and_unknown_rooms_are_not_written(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="3001")
    await _publish(db_session, tenant.id)

    assert await _read(db_session, tenant.id, "1") == RoomFacts(False, False)
    assert await _read(db_session, tenant.id, "2") == RoomFacts(True, False)
    assert await _read(db_session, tenant.id, "3") == RoomFacts(False, True)
    assert await _read(db_session, tenant.id, "9") == UNKNOWN_FACTS
    assert await _read(db_session, tenant.id, "404") == UNKNOWN_FACTS, "no row is unknown"
    assert [
        r.channel_id for r in await store.list_channel_facts(db_session, tenant_id=tenant.id)
    ] == [
        "1",
        "2",
        "3",
    ]


async def test_a_server_with_no_state_is_unknown(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="3002")

    assert await _read(db_session, tenant.id, "1") == UNKNOWN_FACTS
    assert await store.read_status(db_session, tenant_id=tenant.id, now=NOW) is None


async def test_marking_unknown_and_expiry_make_every_room_unknown(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="3003")
    await _publish(db_session, tenant.id)

    assert await _read(db_session, tenant.id, "1", NOW + timedelta(seconds=181)) == UNKNOWN_FACTS, (
        "past fresh_until"
    )
    await store.mark_unknown(db_session, tenant_id=tenant.id, now=LATER)
    assert await _read(db_session, tenant.id, "1") == UNKNOWN_FACTS
    status = await store.read_status(db_session, tenant_id=tenant.id, now=LATER)
    assert status is not None and not status.complete and not status.usable


async def test_a_new_publish_replaces_the_old_set_under_a_new_generation(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="3004")
    first = await _publish(db_session, tenant.id)
    await store.publish_facts(
        db_session,
        tenant_id=tenant.id,
        rooms={"5": StoredRoom(CONTAINED)},
        now=LATER,
        fresh_until=LATER + timedelta(seconds=180),
    )
    status = await store.read_status(db_session, tenant_id=tenant.id, now=LATER)

    assert status is not None and status.generation > first and status.usable
    assert await _read(db_session, tenant.id, "1") == UNKNOWN_FACTS, "the old set is gone"
    assert (await _read(db_session, tenant.id, "5")).contained


async def test_renewal_extends_only_its_own_fresh_set_and_never_revives_a_lapsed_one(
    db_session: AsyncSession,
) -> None:
    fresh = await make_tenant(db_session, platform="discord", workspace_id="3005")
    lapsed = await make_tenant(db_session, platform="discord", workspace_id="3006")
    unknown = await make_tenant(db_session, platform="discord", workspace_id="3007")
    replaced = await make_tenant(db_session, platform="discord", workspace_id="3008")
    generations = {
        fresh.id: await _publish(db_session, fresh.id),
        lapsed.id: await _publish(db_session, lapsed.id, at=NOW - timedelta(seconds=400)),
        unknown.id: await _publish(db_session, unknown.id),
        replaced.id: await _publish(db_session, replaced.id),
    }
    await store.mark_unknown(db_session, tenant_id=unknown.id, now=NOW)
    await _publish(db_session, replaced.id)  # a newer set than the one being renewed

    renewed = {
        tenant_id
        for tenant_id, generation in generations.items()
        if await store.renew_fresh_until(
            db_session,
            tenant_id=tenant_id,
            generation=generation,
            fresh_until=LATER + timedelta(seconds=180),
            now=LATER,
        )
    }

    assert renewed == {fresh.id}
    later = NOW + timedelta(seconds=200)
    assert await _read(db_session, fresh.id, "1", later) == RoomFacts(False, False)
    assert await _read(db_session, lapsed.id, "1", later) == UNKNOWN_FACTS
    assert await _read(db_session, replaced.id, "1", later) == UNKNOWN_FACTS, "not its own set"


async def test_an_uncached_thread_takes_contained_from_its_parent_and_is_narrow(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="3008")
    await _publish(db_session, tenant.id)

    assert await _read(db_session, tenant.id, "70", parent_id="2") == RoomFacts(True, True)
    assert await _read(db_session, tenant.id, "70", parent_id="1") == RoomFacts(False, True)
    assert await _read(db_session, tenant.id, "70", parent_id="404") == UNKNOWN_FACTS


async def test_a_public_thread_with_no_row_takes_both_facts_from_its_parent(
    db_session: AsyncSession,
) -> None:
    """What the next reconcile writes (`compute_room_facts`); a private or unknown kind stays narrow."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="3009")
    await _publish(db_session, tenant.id)

    async def public(parent: str) -> RoomFacts:
        return await _read(db_session, tenant.id, "70", parent_id=parent, public_thread=True)

    assert await public("1") == RoomFacts(False, False)
    assert await public("2") == RoomFacts(True, False)
    assert await public("3") == RoomFacts(False, True)
    assert await public("404") == UNKNOWN_FACTS, "a parent with no row stays unknown"
    # Private (public_thread False): contained from the parent, always narrow.
    assert await _read(db_session, tenant.id, "70", parent_id="1") == RoomFacts(False, True)


async def test_retiring_writes_nothing_for_a_server_without_facts(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="3009")
    await store.retire_facts(db_session, tenant_id=tenant.id, now=NOW)

    assert await store.read_status(db_session, tenant_id=tenant.id, now=NOW) is None
    await _publish(db_session, tenant.id)
    await store.mark_all_unknown(db_session, now=LATER)
    assert await _read(db_session, tenant.id, "1") == UNKNOWN_FACTS


async def test_known_survives_a_database_round_trip_for_every_kind_of_row(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="3010")
    await _publish(db_session, tenant.id)

    open_, contained, narrow, unknown = [
        await _read(db_session, tenant.id, channel) for channel in ("1", "2", "3", "9")
    ]

    assert [f.known for f in (open_, contained, narrow, unknown)] == [True, True, True, False]
    assert (open_.contained, open_.narrow) == (False, False), "a stored open row is known"
    assert (narrow.contained, narrow.narrow) == (False, True)
    assert not (await _read(db_session, tenant.id, "404")).known
    assert (await _read(db_session, tenant.id, "70", parent_id="1")).known, "from the parent"
