"""Turn-card lookup against discord.py's fetched-message component shape."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import discord
import pytest
from daimon.adapters.discord.turn_card_recovery import (
    TurnCardSearchState,
    find_turn_card_message,
    turn_card_custom_id,
    turn_card_ids_from_message,
)

_TURN_ID = UUID("12345678-1234-5678-1234-567812345678")
_CREATED_AFTER = datetime(2026, 9, 25, tzinfo=UTC)
_SEARCH_BEFORE = _CREATED_AFTER + timedelta(minutes=5)


def _fetched_message(message_id: int, *turn_ids: UUID) -> discord.Message:
    """Build the SDK model from a Discord REST message payload."""
    channel = MagicMock()
    channel.id = 456
    channel.guild = None
    components = [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 2,
                    "label": "Cancel",
                    "custom_id": turn_card_custom_id(turn_id),
                }
                for turn_id in turn_ids
            ],
        }
    ]
    payload: dict[str, Any] = {
        "id": str(message_id),
        "type": 0,
        "content": "Working…",
        "channel_id": "456",
        "author": {"id": "123", "username": "Daimon", "discriminator": "0"},
        "components": components,
    }
    return discord.Message(state=MagicMock(), channel=channel, data=payload)  # type: ignore[arg-type]


class _HistoryThread:
    """Small history transport whose yielded groups represent SDK pages."""

    def __init__(self, pages: Sequence[Sequence[discord.Message] | discord.HTTPException]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def history(
        self,
        *,
        after: datetime,
        before: datetime,
        oldest_first: bool,
        limit: int,
    ) -> AsyncIterator[discord.Message]:
        self.calls.append(
            {"after": after, "before": before, "oldest_first": oldest_first, "limit": limit}
        )

        async def iterate() -> AsyncIterator[discord.Message]:
            for page in self.pages:
                if isinstance(page, discord.HTTPException):
                    raise page
                for message in page:
                    yield message

        return iterate()


def test_fetched_message_exposes_custom_id_through_action_row_button() -> None:
    message = _fetched_message(900, _TURN_ID)

    assert turn_card_ids_from_message(message) == frozenset({_TURN_ID}), (
        "discord.py should retain a posted button custom_id when rebuilding Message from REST data"
    )


@pytest.mark.asyncio
async def test_history_lookup_finds_turn_card_across_pages() -> None:
    thread = _HistoryThread(
        [
            [_fetched_message(899)],
            [_fetched_message(900, _TURN_ID)],
        ]
    )

    result = await find_turn_card_message(
        thread, turn_id=_TURN_ID, created_after=_CREATED_AFTER, before=_SEARCH_BEFORE
    )  # type: ignore[arg-type]

    assert result.state is TurnCardSearchState.FOUND, "one matching message should be found"
    assert result.message_ids == (900,), "the matching Discord message ID should be returned"
    assert thread.calls == [
        {
            "after": _CREATED_AFTER - timedelta(seconds=1),
            "before": _SEARCH_BEFORE,
            "oldest_first": True,
            "limit": 1000,
        }
    ], "the lookup should include the intent's millisecond bucket and bound the search window"


@pytest.mark.asyncio
async def test_history_lookup_reports_multiple_matching_messages() -> None:
    thread = _HistoryThread([[_fetched_message(901, _TURN_ID)], [_fetched_message(900, _TURN_ID)]])

    result = await find_turn_card_message(
        thread, turn_id=_TURN_ID, created_after=_CREATED_AFTER, before=_SEARCH_BEFORE
    )  # type: ignore[arg-type]

    assert result.state is TurnCardSearchState.MULTIPLE, (
        "two cards carrying one durable turn ID must be reported as ambiguous"
    )
    assert result.message_ids == (900, 901), "all matching message IDs should be retained"


@pytest.mark.asyncio
async def test_history_api_failure_after_match_is_indeterminate() -> None:
    response = MagicMock()
    response.status = 403
    forbidden = discord.Forbidden(response, "missing read-history permission")
    thread = _HistoryThread([[_fetched_message(900, _TURN_ID)], forbidden])

    result = await find_turn_card_message(
        thread, turn_id=_TURN_ID, created_after=_CREATED_AFTER, before=_SEARCH_BEFORE
    )  # type: ignore[arg-type]

    assert result.state is TurnCardSearchState.INDETERMINATE, (
        "an unread history page means duplicate count is unknown even after a match"
    )
    assert result.message_ids == (900,), "partial evidence should remain inspectable"


@pytest.mark.asyncio
async def test_exhaustive_history_without_match_is_only_reported_as_not_found() -> None:
    thread = _HistoryThread([[_fetched_message(899)], [_fetched_message(900)]])

    result = await find_turn_card_message(
        thread, turn_id=_TURN_ID, created_after=_CREATED_AFTER, before=_SEARCH_BEFORE
    )  # type: ignore[arg-type]

    assert result.state is TurnCardSearchState.NOT_FOUND, (
        "the lookup reports absence without deciding whether to post another card"
    )
    assert result.message_ids == (), "nonmatching messages should not be returned"


@pytest.mark.asyncio
async def test_history_lookup_reports_indeterminate_when_message_budget_is_exhausted() -> None:
    thread = _HistoryThread([[_fetched_message(899), _fetched_message(900)]])

    result = await find_turn_card_message(
        thread,
        turn_id=_TURN_ID,
        created_after=_CREATED_AFTER,
        before=_SEARCH_BEFORE,
        message_budget=2,
    )  # type: ignore[arg-type]

    assert result.state is TurnCardSearchState.INDETERMINATE, (
        "reaching the scan budget cannot prove that no later matching card exists"
    )
