"""Post durable initial turn cards and find them in Discord history.

The lookup reports ambiguity and incomplete history reads explicitly. It does
not decide whether a missing card should be posted or whether a found card
should be edited.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

import structlog
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.core.stores.domain import TurnCardIntentRow
from daimon.core.stores.turn_card_intents import (
    create_turn_card_intent,
    record_turn_card_message,
    retire_turn_card_intent,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord
from discord.components import ActionRow, Button

log = structlog.get_logger()


async def post_initial_turn_card(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    thread_id: str,
    make_lifecycle: Callable[
        [UUID, Callable[[discord.Message], Awaitable[None]]], DiscordTurnLifecycle
    ],
) -> tuple[TurnCardIntentRow, DiscordTurnLifecycle]:
    """Commit a durable intent before posting, then persist Discord's response ID.

    If Discord accepts the post but the response is lost, or the message-ID
    commit fails, the prepared row remains for later history lookup. The caller
    must not start an explicitly prompted MA turn unless this function returns
    successfully. Unprompted turns may defer their first visible post until the
    render loop; that post still commits its response ID before the render hook
    returns.
    """
    async with sessionmaker() as session:
        intent = await create_turn_card_intent(
            session,
            tenant_id=tenant_id,
            platform="discord",
            thread_id=thread_id,
            turn_token=uuid4(),
        )
        await session.commit()

    async def record_posted_message(message: discord.Message) -> None:
        async with sessionmaker() as session:
            recorded = await record_turn_card_message(
                session, id=intent.id, message_id=str(message.id)
            )
            if not recorded:
                raise RuntimeError(
                    "Discord initial turn card intent no longer accepts its message ID"
                )
            await session.commit()

    lifecycle = make_lifecycle(intent.id, record_posted_message)
    await lifecycle.post_initial()
    return intent, lifecycle


async def retire_terminal_turn_card(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    intent_id: UUID,
    expected_message_id: str | None,
) -> bool:
    """Retire a completed card only when its persisted Discord ID still matches."""
    if expected_message_id is None:
        return False
    try:
        async with sessionmaker() as session:
            retired = await retire_turn_card_intent(
                session, id=intent_id, expected_message_id=expected_message_id
            )
            await session.commit()
        return retired
    except SQLAlchemyError:
        log.warning(
            "turn.card_intent_retire_failed",
            intent_id=str(intent_id),
            message_id=expected_message_id,
            exc_info=True,
        )
        return False


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
    before: datetime,
    message_budget: int = 1000,
) -> TurnCardSearchResult:
    """Scan a bounded time and message window for this turn ID.

    discord.py turns a datetime bound into a millisecond snowflake. Start one
    second earlier so a card posted in the intent's timestamp bucket is not
    skipped. The turn ID still filters unrelated messages. discord.py paginates
    the ``limit=None`` history iterator. Any HTTP failure
    makes the result indeterminate, even if a matching message was already
    yielded, because unread pages could contain a duplicate. Reaching the
    message budget is also indeterminate: unread messages could contain a
    duplicate.
    """
    if message_budget <= 0:
        raise ValueError("message_budget must be positive")
    message_ids: set[int] = set()
    messages_read = 0
    try:
        async for message in thread.history(
            after=created_after - timedelta(seconds=1),
            before=before,
            oldest_first=True,
            limit=message_budget,
        ):
            messages_read += 1
            if turn_id in turn_card_ids_from_message(message):
                message_ids.add(message.id)
    except discord.HTTPException:
        return TurnCardSearchResult(
            state=TurnCardSearchState.INDETERMINATE,
            message_ids=tuple(sorted(message_ids)),
        )

    if messages_read == message_budget:
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
