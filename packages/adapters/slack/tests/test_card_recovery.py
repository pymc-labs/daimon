from __future__ import annotations

import datetime as dt
import re

from aioresponses import aioresponses as AioResponsesMock
from daimon.adapters.slack.blockkit import State, to_blocks
from daimon.adapters.slack.card_recovery import (
    CardLookupStatus,
    find_turn_card_by_key,
)
from slack_sdk.web.async_client import AsyncWebClient

_REPLIES = re.compile(r"https://slack\.com/api/conversations\.replies.*")
_KEY = "turn-intent-uuid"
_INTENT_CREATED_AT = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
_SEARCH_OLDEST = f"{(_INTENT_CREATED_AT.timestamp() - 300):.6f}"


def _message(ts: str, *, key: str | None = None) -> dict[str, object]:
    blocks: list[dict[str, object]] = []
    if key is not None:
        blocks.append(
            {
                "type": "actions",
                "elements": [{"type": "button", "action_id": "cancel_turn", "value": key}],
            }
        )
    return {"ts": ts, "blocks": blocks}


def _page(messages: list[dict[str, object]], *, cursor: str = "") -> dict[str, object]:
    return {
        "ok": True,
        "messages": messages,
        "has_more": bool(cursor),
        "response_metadata": {"next_cursor": cursor},
    }


def _client() -> AsyncWebClient:
    return AsyncWebClient(token="xoxb-test")


async def test_finds_card_by_cancel_button_value() -> None:
    blocks = to_blocks(State(), now=1, cancel_key=_KEY)
    with AioResponsesMock() as mock:
        mock.get(
            _REPLIES,
            payload=_page([{"ts": "100.2", "blocks": blocks}, _message("100.1")]),
        )

        result = await find_turn_card_by_key(
            _client(),
            channel="C1",
            thread_ts="100.0",
            cancel_key=_KEY,
            intent_created_at=_INTENT_CREATED_AT,
        )

    assert result.status is CardLookupStatus.FOUND
    assert result.message_ts == "100.2"


async def test_paginates_past_fifteen_messages_to_find_card() -> None:
    first_page = [_message(f"100.{i}") for i in range(15)]
    with AioResponsesMock() as mock:
        mock.get(_REPLIES, payload=_page(first_page, cursor="page-two"))
        mock.get(_REPLIES, payload=_page([_message("100.16", key=_KEY)]))

        result = await find_turn_card_by_key(
            _client(),
            channel="C1",
            thread_ts="100.0",
            cancel_key=_KEY,
            intent_created_at=_INTENT_CREATED_AT,
        )
        requests = [
            dict(url.query)
            for (method, url), _ in mock.requests.items()
            if method == "GET" and url.path == "/api/conversations.replies"
        ]

    assert result.status is CardLookupStatus.FOUND
    assert result.message_ts == "100.16"
    assert len(requests) == 2
    assert requests[0]["limit"] == "15"
    assert requests[0]["oldest"] == _SEARCH_OLDEST
    assert requests[1]["cursor"] == "page-two"


async def test_reports_duplicate_key_across_pages_as_ambiguous() -> None:
    with AioResponsesMock() as mock:
        mock.get(_REPLIES, payload=_page([_message("100.2", key=_KEY)], cursor="next"))
        mock.get(_REPLIES, payload=_page([_message("100.3", key=_KEY)]))

        result = await find_turn_card_by_key(
            _client(),
            channel="C1",
            thread_ts="100.0",
            cancel_key=_KEY,
            intent_created_at=_INTENT_CREATED_AT,
        )

    assert result.status is CardLookupStatus.MULTIPLE
    assert result.message_ts is None
    assert result.reason == "duplicate_key"


async def test_api_failure_is_indeterminate() -> None:
    with AioResponsesMock() as mock:
        mock.get(_REPLIES, status=500, payload={"ok": False, "error": "internal_error"})

        result = await find_turn_card_by_key(
            _client(),
            channel="C1",
            thread_ts="100.0",
            cancel_key=_KEY,
            intent_created_at=_INTENT_CREATED_AT,
        )

    assert result.status is CardLookupStatus.INDETERMINATE
    assert result.message_ts is None
    assert result.reason == "api_error"


async def test_more_without_a_cursor_is_indeterminate() -> None:
    with AioResponsesMock() as mock:
        mock.get(_REPLIES, payload={"ok": True, "messages": [], "has_more": True})

        result = await find_turn_card_by_key(
            _client(),
            channel="C1",
            thread_ts="100.0",
            cancel_key=_KEY,
            intent_created_at=_INTENT_CREATED_AT,
        )

    assert result.status is CardLookupStatus.INDETERMINATE
    assert result.reason == "missing_cursor"


async def test_scan_budget_exhaustion_is_indeterminate() -> None:
    with AioResponsesMock() as mock:
        mock.get(_REPLIES, payload=_page([], cursor="page-two"))
        mock.get(_REPLIES, payload=_page([], cursor="page-three"))

        result = await find_turn_card_by_key(
            _client(),
            channel="C1",
            thread_ts="100.0",
            cancel_key=_KEY,
            intent_created_at=_INTENT_CREATED_AT,
        )

    assert result.status is CardLookupStatus.INDETERMINATE
    assert result.reason == "scan_limit"
