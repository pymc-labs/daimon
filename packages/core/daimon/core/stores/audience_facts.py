"""Store for the Discord room facts: one complete set per server, or unknown.

The Discord bot computes a server's facts (`daimon.core.audience_facts`) and writes them
here in one transaction with a new generation. Other processes read one room at a time.
A server with no state, an incomplete state, an expired `fresh_until` or no row for the
room is unknown, and unknown is contained and narrow.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from daimon.core._models import DiscordAudienceState, DiscordChannelAudience
from daimon.core.audience_facts import UNKNOWN_FACTS, RoomFacts
from sqlalchemy import and_, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

_log = structlog.get_logger(__name__)
_BATCH = 1000


@dataclass(frozen=True)
class StoredRoom:
    """A room's facts with its parent, for a thread."""

    facts: RoomFacts
    parent_id: str | None = None


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
    tenant_id: uuid.UUID,
    generation: int,
    fresh_until: datetime,
    now: datetime,
) -> bool:
    """Push the server's `fresh_until` out to `fresh_until`, if its facts are still the
    complete, fresh set of `generation`. Whether it was renewed.

    The caller bounds the limit by the start of the read the facts came from. A lapsed,
    retired or replaced set is not touched: it stays as it is until the next full publish.
    """
    done = await session.execute(
        update(DiscordAudienceState)
        .where(
            DiscordAudienceState.tenant_id == tenant_id,
            DiscordAudienceState.generation == generation,
            DiscordAudienceState.complete.is_(True),
            DiscordAudienceState.fresh_until > now,
        )
        .values(fresh_until=fresh_until)
        .returning(DiscordAudienceState.tenant_id)
    )
    return done.scalar_one_or_none() is not None


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


#: A turn waits at most this long for the facts to come back (mode `on` only).
FACTS_WAIT_S = 20.0
#: Pause between two reads of the state row while a turn waits.
FACTS_WAIT_POLL_S = 0.5
#: A retire or lapse older than this is an outage, not a short gap: no wait.
FACTS_WAIT_RECENT_S = 60.0
#: The most a caller's `on_wait` callback may take.
FACTS_WAIT_CALLBACK_S = 2.0


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _clock() -> float:
    return time.monotonic()


def _publish_expected(status: AudienceStatus | None, now: datetime) -> bool:
    """Unusable facts retired, or lapsed, only moments ago: a publish is about to come."""
    if status is None or status.usable:
        return False
    since = status.updated_at if not status.complete else status.fresh_until
    return since is not None and now - since <= timedelta(seconds=FACTS_WAIT_RECENT_S)


async def wait_for_usable_facts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    budget_s: float | None = None,
    on_wait: Callable[[], Awaitable[None]] | None = None,
) -> float | None:
    """Wait for the server's facts to be usable when a fresh publish is expected soon.

    No wait, and None, when the facts are usable, were never written, were retired or
    lapsed more than `FACTS_WAIT_RECENT_S` ago, or `budget_s` (the part of `FACTS_WAIT_S`
    this turn has left; None is all of it) is spent. Otherwise `on_wait` is awaited once (for
    at most `FACTS_WAIT_CALLBACK_S`; its failure is only logged) and the transaction is
    committed, so no connection or lock is held while it sleeps. The state row is read about
    every `FACTS_WAIT_POLL_S` until the facts are usable or the elapsed time since the call
    began, reads included, reaches the budget. The bound is on the waiting: a database read
    in flight is finished, never cancelled (a cancelled read can leave the session
    unusable), so a slow database can run a little past it. Returns the elapsed seconds; a
    caller that still finds the facts unusable fails closed as before. One
    `audience.facts_wait` log.
    """
    budget = FACTS_WAIT_S if budget_s is None else min(budget_s, FACTS_WAIT_S)
    if budget <= 0:
        return None
    started = _clock()
    status = await read_status(session, tenant_id=tenant_id, now=datetime.now(UTC))
    if not _publish_expected(status, datetime.now(UTC)):
        return None
    if on_wait is not None:
        try:
            await asyncio.wait_for(on_wait(), timeout=FACTS_WAIT_CALLBACK_S)
        except Exception:  # best effort; a slow or failed callback never holds admission
            _log.debug("audience.facts_wait_callback_failed", exc_info=True)
    await session.commit()
    usable = False
    while _clock() - started < budget and not usable:
        await _pause(min(FACTS_WAIT_POLL_S, max(budget - (_clock() - started), 0.0)))
        status = await read_status(session, tenant_id=tenant_id, now=datetime.now(UTC))
        await session.commit()
        usable = status is not None and status.usable
    waited = _clock() - started
    _log.info(
        "audience.facts_wait",
        tenant_id=str(tenant_id),
        waited_s=round(waited, 1),
        result="usable" if usable else "gave_up",
    )
    return waited


async def read_channel_facts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    now: datetime,
    parent_id: str | None = None,
    public_thread: bool = False,
) -> RoomFacts:
    """One room's facts: unknown when the server is incomplete or lapsed or the room has no row.

    A thread with no row (an archived one is not cached, a new one has none for seconds) that
    names its `parent_id` takes the parent's "contained" and is narrow, unless the caller knows
    from the platform that it is a `public_thread` (a forum post and an announcement thread
    too): then it takes both facts from the parent, as the next reconcile writes them
    (`compute_room_facts`). A private thread, and a thread of unknown kind, is always narrow.
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
        return RoomFacts(contained=parent[0], narrow=parent[1] if public_thread else True)
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
