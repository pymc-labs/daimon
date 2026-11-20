"""Store for the Discord room facts: one complete set per server, or unknown.

The Discord bot computes a server's facts (`daimon.core.audience_facts`) and writes them
here in one transaction with a new generation. Other processes read one room at a time.
A server with no state, an incomplete state, an expired `fresh_until` or no row for the
room is unknown, and unknown is contained and narrow.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime

from daimon.core._models import DiscordAudienceState, DiscordChannelAudience
from daimon.core.audience_facts import UNKNOWN_FACTS, RoomFacts
from sqlalchemy import and_, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

_BATCH = 1000


@dataclass(frozen=True)
class StoredRoom:
    """A room's facts with its parent, for a thread."""

    facts: RoomFacts
    parent_id: str | None = None


def merge_with_baseline(
    cache: Mapping[str, StoredRoom], baseline: Mapping[str, StoredRoom] | None
) -> dict[str, StoredRoom]:
    """Rooms computed from the gateway cache, never more open than the last reconcile saw.

    A room also in `baseline` is contained or narrow if either says so, so only the next
    reconcile can relax what a reconcile found. A room not in `baseline` (new since) keeps
    the cache result, and a room missing from `cache` is dropped. A thread is then never
    more open than its merged parent, because a new thread inherits a parent the cache may
    hold stale. Pure.
    """
    merged = {
        channel_id: _tighter(room, (baseline or {}).get(channel_id))
        for channel_id, room in cache.items()
    }
    for channel_id, room in merged.items():
        parent = merged.get(room.parent_id) if room.parent_id is not None else None
        if parent is not None and parent.parent_id is None:
            merged[channel_id] = _tighter(room, parent)
    return merged


def _tighter(room: StoredRoom, other: StoredRoom | None) -> StoredRoom:
    """`room`, contained or narrow where `other` is too. An unknown room stays unknown."""
    if other is None or not room.facts.known:
        return room
    facts = room.facts
    return StoredRoom(
        RoomFacts(
            contained=facts.contained or other.facts.contained,
            narrow=facts.narrow or other.facts.narrow,
            outsiders_count=facts.outsiders_count,
            team_shut_out_count=facts.team_shut_out_count,
        ),
        room.parent_id,
    )


@dataclass(frozen=True)
class AudienceStatus:
    """A server's state row, read at `now`."""

    complete: bool
    generation: int
    fresh_until: datetime | None
    updated_at: datetime
    usable: bool
    """Complete and not yet past `fresh_until`: its room rows can be trusted."""


async def publish_facts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    rooms: Mapping[str, StoredRoom],
    now: datetime,
    fresh_until: datetime,
) -> int:
    """Replace the server's facts with a full set, complete, under a new generation.

    One transaction in the caller's session. Rooms with unknown facts are not written:
    a room with no row is unknown. Returns the new generation.
    """
    generation = await _bump(session, tenant_id, complete=True, fresh_until=fresh_until, now=now)
    await session.execute(
        delete(DiscordChannelAudience).where(DiscordChannelAudience.tenant_id == tenant_id)
    )
    rows = [
        {
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "parent_id": room.parent_id,
            "contained": room.facts.contained,
            "narrow": room.facts.narrow,
            "generation": generation,
            "computed_at": now,
        }
        for channel_id, room in rooms.items()
        if room.facts.known
    ]
    for start in range(0, len(rows), _BATCH):
        await session.execute(insert(DiscordChannelAudience), rows[start : start + _BATCH])
    return generation


async def mark_unknown(session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime) -> None:
    """Make every room of the server unknown, until the next full publish."""
    await _bump(session, tenant_id, complete=False, fresh_until=None, now=now)


async def _bump(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    complete: bool,
    fresh_until: datetime | None,
    now: datetime,
) -> int:
    new = insert(DiscordAudienceState).values(
        tenant_id=tenant_id,
        complete=complete,
        generation=1,
        fresh_until=fresh_until,
        updated_at=now,
    )
    row = await session.execute(
        new.on_conflict_do_update(
            index_elements=[DiscordAudienceState.tenant_id],
            set_={
                "complete": complete,
                "generation": DiscordAudienceState.generation + 1,
                "fresh_until": fresh_until,
                "updated_at": now,
            },
        ).returning(DiscordAudienceState.generation)
    )
    return row.scalar_one()


async def renew_fresh_until(
    session: AsyncSession,
    *,
    fresh_until: Mapping[uuid.UUID, datetime],
    now: datetime,
) -> set[uuid.UUID]:
    """Push `fresh_until` out to each server's own limit, for each complete, still fresh
    server among the keys.

    The caller bounds each limit by the server's last full reconcile. A lapsed server is
    not revived: it stays unknown until the next full publish. Returns the servers renewed.
    """
    renewed: set[uuid.UUID] = set()
    for tenant_id, limit in fresh_until.items():
        done = await session.execute(
            update(DiscordAudienceState)
            .where(
                DiscordAudienceState.tenant_id == tenant_id,
                DiscordAudienceState.complete.is_(True),
                DiscordAudienceState.fresh_until > now,
            )
            .values(fresh_until=limit)
            .returning(DiscordAudienceState.tenant_id)
        )
        renewed.update(done.scalars())
    return renewed


async def retire_facts(session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime) -> None:
    """Make the server's facts unknown if it has any, and write nothing if it has none."""
    await session.execute(
        update(DiscordAudienceState)
        .where(DiscordAudienceState.tenant_id == tenant_id, DiscordAudienceState.complete.is_(True))
        .values(
            complete=False,
            fresh_until=None,
            generation=DiscordAudienceState.generation + 1,
            updated_at=now,
        )
    )


async def mark_all_unknown(session: AsyncSession, *, now: datetime) -> None:
    """Make every server's facts unknown: the gateway no longer sends the events that keep
    them current."""
    await session.execute(
        update(DiscordAudienceState)
        .where(DiscordAudienceState.complete.is_(True))
        .values(
            complete=False,
            fresh_until=None,
            generation=DiscordAudienceState.generation + 1,
            updated_at=now,
        )
    )


async def read_status(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime
) -> AudienceStatus | None:
    """The server's state row, or None when none was ever written."""
    state = (
        await session.execute(
            select(DiscordAudienceState).where(DiscordAudienceState.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if state is None:
        return None
    return AudienceStatus(
        complete=state.complete,
        generation=state.generation,
        fresh_until=state.fresh_until,
        updated_at=state.updated_at,
        usable=state.complete and state.fresh_until is not None and state.fresh_until > now,
    )


async def read_channel_facts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    now: datetime,
    parent_id: str | None = None,
) -> RoomFacts:
    """One room's facts: unknown when the server is incomplete or lapsed or the room has no row.

    A thread with no row (an archived one is not cached) that names its `parent_id` takes
    the parent's "contained" and is narrow: both kinds of thread take "contained" from the
    parent, and a private one is always narrow.
    """
    found = await read_channel_rows(
        session,
        tenant_id=tenant_id,
        channel_ids=[channel_id, *([parent_id] if parent_id else [])],
        now=now,
    )
    own = found.get(channel_id)
    if own is not None:
        return RoomFacts(contained=own[0], narrow=own[1])
    parent = found.get(parent_id) if parent_id else None
    if parent is not None:
        return RoomFacts(contained=parent[0], narrow=True)
    return UNKNOWN_FACTS


async def read_channel_rows(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_ids: Collection[str],
    now: datetime,
) -> dict[str, tuple[bool, bool]]:
    """Channel id to (contained, narrow) for the ids with a row of the current complete
    generation; empty when the server is incomplete, lapsed or has no state."""
    rows = await session.execute(
        select(
            DiscordChannelAudience.channel_id,
            DiscordChannelAudience.contained,
            DiscordChannelAudience.narrow,
        )
        .join(
            DiscordAudienceState,
            and_(
                DiscordAudienceState.tenant_id == DiscordChannelAudience.tenant_id,
                DiscordAudienceState.generation == DiscordChannelAudience.generation,
            ),
        )
        .where(
            DiscordChannelAudience.tenant_id == tenant_id,
            DiscordChannelAudience.channel_id.in_(list(channel_ids)),
            DiscordAudienceState.complete.is_(True),
            DiscordAudienceState.fresh_until > now,
        )
    )
    return {channel_id: (contained, narrow) for channel_id, contained, narrow in rows.tuples()}


@dataclass(frozen=True)
class StoredChannelFact:
    channel_id: str
    parent_id: str | None
    contained: bool
    narrow: bool


async def list_channel_facts(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[StoredChannelFact]:
    """Every stored room row of the current generation, by channel id. For the report; the
    caller checks `read_status` before it trusts them."""
    rows = await session.execute(
        select(DiscordChannelAudience)
        .join(
            DiscordAudienceState,
            and_(
                DiscordAudienceState.tenant_id == DiscordChannelAudience.tenant_id,
                DiscordAudienceState.generation == DiscordChannelAudience.generation,
            ),
        )
        .where(DiscordChannelAudience.tenant_id == tenant_id)
        .order_by(DiscordChannelAudience.channel_id)
    )
    return [
        StoredChannelFact(r.channel_id, r.parent_id, r.contained, r.narrow) for r in rows.scalars()
    ]
