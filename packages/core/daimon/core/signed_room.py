"""The `room` claim of a signed MCP call: what kind of place the answer goes to.

A server that knows the claim can limit what it returns to what that place may read. The
claim states only the kind of place, from Daimon's own room facts
(`daimon.core.audience_facts`); it carries no rule of any one workspace:

- `dm`: a direct message with the person asking, known from the channel's type alone.
- `contained`: a person from outside can view the room, or its facts are unknown.
- `narrow`: the room shuts out several team members or someone by name, or is a
  private thread.
- `team`: any other room.

Contained wins over narrow. The facts are read when the call is signed, for the place the
answer is posted to, whatever the server's `audience_hold` mode says: a room that changed
between two turns carries its new kind on the next call. Only Discord rooms have facts;
any other platform sends no claim.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Final, Literal

import structlog
from daimon.core.audience_facts import RoomFacts
from daimon.core.stores.audience_facts import read_channel_facts
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.turn_origin import origin_place
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

SignedRoom = Literal["dm", "team", "narrow", "contained"]
#: Platforms whose rooms have facts, so a signed call there can name its room.
ROOM_PLATFORMS: Final = frozenset({"discord"})


def room_of(facts: RoomFacts) -> SignedRoom:
    """The kind of a room that is not a DM: contained (or unknown) first, then narrow."""
    if facts.contained or not facts.known:
        return "contained"
    return "narrow" if facts.narrow else "team"


async def _place_room(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    room: str,
    parent: str | None,
    public_thread: bool,
    now: datetime,
) -> SignedRoom:
    """The kind of a room or thread by its stored facts. A public thread with no fact of its
    own yet takes its parent's; any other thread with none is narrow (`read_channel_facts`).
    A fact that cannot be read makes the room contained."""
    try:
        async with sessionmaker() as session:
            facts = await read_channel_facts(
                session,
                tenant_id=tenant_id,
                channel_id=room,
                parent_id=parent if parent != room else None,
                public_thread=public_thread,
                now=now,
            )
    except Exception as err:  # boundary: an unreadable room counts as contained
        log.warning("signed_room.facts_unreadable", error_type=type(err).__name__)
        return "contained"
    return room_of(facts)


async def turn_room(
    sessionmaker: async_sessionmaker[AsyncSession], origin: TurnOriginRow, *, now: datetime
) -> SignedRoom | None:
    """The kind of place a turn's answer goes to: its thread, or its channel when they are
    the same. None off Discord."""
    if origin.platform not in ROOM_PLATFORMS:
        return None
    if origin_place(origin) == "dm":
        return "dm"
    return await _place_room(
        sessionmaker,
        tenant_id=origin.tenant_id,
        room=origin.thread_id,
        parent=origin.parent_channel_id,
        public_thread=origin.public_thread,
        now=now,
    )


async def routine_room(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    destination: Mapping[str, str | None] | None,
    now: datetime,
) -> tuple[str, SignedRoom] | None:
    """The room a routine posts to and its kind, from its `routine_conversation`.

    None off Discord, for a DM destination and for no destination: such a run names no
    room. The thread kind is not recorded, so a thread with no fact of its own is narrow.
    """
    if platform not in ROOM_PLATFORMS or destination is None:
        return None
    channel = destination.get("channel_id")
    if destination.get("place_type") not in ("channel", "thread") or not channel:
        return None
    room = await _place_room(
        sessionmaker,
        tenant_id=tenant_id,
        room=channel,
        parent=destination.get("parent_channel_id"),
        public_thread=False,
        now=now,
    )
    return channel, room
