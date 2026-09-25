"""Find Discord messages carrying the durable ID of an initial turn card.

The lookup reports ambiguity and incomplete history reads explicitly. It does
not decide whether a missing card should be posted or whether a found card
should be edited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

import discord
from discord.components import ActionRow, Button

_TURN_CARD_CUSTOM_ID_PREFIX = "daimon:cancel:"


class TurnCardSearchState(StrEnum):
    """Outcome of an exhaustive scan for a turn card in thread history."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    MULTIPLE = "multiple"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class TurnCardSearchResult:
    """Message IDs matching a turn ID and whether the history scan was conclusive."""

    state: TurnCardSearchState
    message_ids: tuple[int, ...]


def turn_card_custom_id(turn_id: UUID) -> str:
    """Encode a durable turn ID in the existing Cancel button's custom ID."""
    return f"{_TURN_CARD_CUSTOM_ID_PREFIX}{turn_id}"


def _turn_id_from_custom_id(custom_id: str | None) -> UUID | None:
    if custom_id is None or not custom_id.startswith(_TURN_CARD_CUSTOM_ID_PREFIX):
        return None
    value = custom_id.removeprefix(_TURN_CARD_CUSTOM_ID_PREFIX)
    try:
        turn_id = UUID(value)
    except ValueError:
        return None
    if str(turn_id) != value:
        return None
    return turn_id


def turn_card_ids_from_message(message: discord.Message) -> frozenset[UUID]:
    """Extract turn IDs from standard action-row buttons on a fetched message."""
    turn_ids: set[UUID] = set()
    for component in message.components:
        if not isinstance(component, ActionRow):
            continue
        for child in component.children:
            if not isinstance(child, Button):
                continue
            turn_id = _turn_id_from_custom_id(child.custom_id)
            if turn_id is not None:
                turn_ids.add(turn_id)
    return frozenset(turn_ids)


async def find_turn_card_message(
    thread: discord.Thread,
    *,
    turn_id: UUID,
    created_after: datetime,
) -> TurnCardSearchResult:
    """Scan all newer thread messages for this turn ID.

    discord.py turns a datetime bound into a millisecond snowflake. Start one
    second earlier so a card posted in the intent's timestamp bucket is not
    skipped. The turn ID still filters unrelated messages. discord.py paginates
    the ``limit=None`` history iterator. Any HTTP failure
    makes the result indeterminate, even if a matching message was already
    yielded, because unread pages could contain a duplicate.
    """
    message_ids: set[int] = set()
    try:
        async for message in thread.history(
            after=created_after - timedelta(seconds=1), oldest_first=True, limit=None
        ):
            if turn_id in turn_card_ids_from_message(message):
                message_ids.add(message.id)
    except discord.HTTPException:
        return TurnCardSearchResult(
            state=TurnCardSearchState.INDETERMINATE,
            message_ids=tuple(sorted(message_ids)),
        )

    ordered_ids = tuple(sorted(message_ids))
    if not ordered_ids:
        return TurnCardSearchResult(state=TurnCardSearchState.NOT_FOUND, message_ids=())
    if len(ordered_ids) == 1:
        return TurnCardSearchResult(state=TurnCardSearchState.FOUND, message_ids=ordered_ids)
    return TurnCardSearchResult(state=TurnCardSearchState.MULTIPLE, message_ids=ordered_ids)
