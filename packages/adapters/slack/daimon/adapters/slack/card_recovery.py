"""Read-only lookup for a running turn's status card in Slack history.

The Cancel button carries a stable per-turn value. A process restart can use
that value to locate the card after Slack has returned the message timestamp.
History lookup is deliberately tri-state around absence: an API or pagination
failure cannot establish that no matching card exists.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import enum
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast

import aiohttp
from slack_sdk.errors import SlackApiError, SlackRequestError
from slack_sdk.web.async_client import AsyncWebClient

# This remains compatible with new commercially distributed, unlisted Slack
# installs, which are limited to 15 messages per request. Slack's official
# docs exempt Marketplace apps, internal customer-built apps, and existing
# unlisted installations from that reduced limit.
_PAGE_LIMIT = 15
_PAGE_BUDGET = 3
_PAGE_TIMEOUT_SECONDS = 8.0
_PAGE_PACING_SECONDS = 60.0
_INTENT_TIME_MARGIN_SECONDS = 60


class CardLookupStatus(enum.StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    MULTIPLE = "multiple"
    INDETERMINATE = "indeterminate"


@dataclasses.dataclass(frozen=True)
class CardLookup:
    status: CardLookupStatus
    message_ts: str | None = None
    message_timestamps: tuple[str, ...] = ()
    reason: str | None = None
    retry_after_seconds: float | None = None


class _SlackErrorResponse(Protocol):
    headers: Mapping[str, str]
    status_code: int


def _as_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def _has_cancel_key(message: dict[str, object], *, cancel_key: str) -> bool:
    blocks = message.get("blocks")
    if not isinstance(blocks, list):
        return False
    for raw_block in cast(list[object], blocks):
        block = _as_mapping(raw_block)
        if block is None or block.get("type") != "actions":
            continue
        elements = block.get("elements")
        if not isinstance(elements, list):
            continue
        for raw_element in cast(list[object], elements):
            element = _as_mapping(raw_element)
            if (
                element is not None
                and element.get("type") == "button"
                and element.get("action_id") == "cancel_turn"
                and element.get("value") == cancel_key
            ):
                return True
    return False


async def find_turn_card_by_key(
    client: AsyncWebClient,
    *,
    channel: str,
    thread_ts: str,
    cancel_key: str,
    intent_created_at: dt.datetime,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: dt.datetime | None = None,
    time_window: tuple[dt.datetime, dt.datetime] | None = None,
) -> CardLookup:
    """Find a card by its Cancel value in a bounded search interval.

    ``NOT_FOUND`` is returned only after Slack reports the bounded interval
    complete. API errors, malformed pages, and broken cursor chains return
    ``INDETERMINATE`` so callers cannot mistake an incomplete read for absence.
    ``time_window`` may bound a known message timestamp directly. Otherwise
    the search covers one minute before intent creation through five minutes
    after it; a post accepted later than that window can be missed under this
    bounded recovery policy.

    At most three pages are read, with an eight-second timeout per page and at
    least sixty seconds between requests. This accommodates Slack's reduced
    one-request-per-minute limit for some distributed apps. If Slack reports
    more history beyond that budget, the result is ``INDETERMINATE``. Multiple
    matches return every timestamp so callers can reconcile duplicate cards.
    """
    cursor: str | None = None
    seen_cursors: set[str] = set()
    matches: list[str] = []
    if intent_created_at.tzinfo is None or intent_created_at.utcoffset() is None:
        return CardLookup(CardLookupStatus.INDETERMINATE, reason="invalid_intent_time")
    scan_latest = now or dt.datetime.now(dt.UTC)
    if scan_latest.tzinfo is None or scan_latest.utcoffset() is None:
        return CardLookup(CardLookupStatus.INDETERMINATE, reason="invalid_scan_time")
    if time_window is None:
        scan_oldest = intent_created_at - dt.timedelta(seconds=_INTENT_TIME_MARGIN_SECONDS)
        bounded_latest = intent_created_at + dt.timedelta(seconds=300)
    else:
        scan_oldest, bounded_latest = time_window
        if (
            scan_oldest.tzinfo is None
            or scan_oldest.utcoffset() is None
            or bounded_latest.tzinfo is None
            or bounded_latest.utcoffset() is None
            or bounded_latest < scan_oldest
        ):
            return CardLookup(CardLookupStatus.INDETERMINATE, reason="invalid_scan_window")
    scan_latest = min(scan_latest, bounded_latest)
    if scan_latest < intent_created_at or scan_latest < scan_oldest:
        return CardLookup(CardLookupStatus.INDETERMINATE, reason="invalid_scan_window")
    oldest = f"{scan_oldest.timestamp():.6f}"
    latest = f"{scan_latest.timestamp():.6f}"

    while True:
        if seen_cursors:
            await sleep(_PAGE_PACING_SECONDS)
        try:
            kwargs: dict[str, Any] = {
                "channel": channel,
                "ts": thread_ts,
                "oldest": oldest,
                "latest": latest,
                "limit": _PAGE_LIMIT,
            }
            if cursor is not None:
                kwargs["cursor"] = cursor
            async with asyncio.timeout(_PAGE_TIMEOUT_SECONDS):
                response = await client.conversations_replies(  # pyright: ignore[reportUnknownMemberType]
                    **kwargs
                )
        except TimeoutError:
            return CardLookup(CardLookupStatus.INDETERMINATE, reason="api_timeout")
        except SlackApiError as error:
            response = cast(_SlackErrorResponse, error.response)
            retry_after_header = response.headers.get("Retry-After")
            try:
                retry_after_seconds = (
                    float(retry_after_header) if retry_after_header is not None else None
                )
            except ValueError:
                retry_after_seconds = None
            return CardLookup(
                CardLookupStatus.INDETERMINATE,
                reason="rate_limited" if response.status_code == 429 else "api_error",
                retry_after_seconds=retry_after_seconds,
            )
        except (SlackRequestError, aiohttp.ClientError):
            return CardLookup(CardLookupStatus.INDETERMINATE, reason="api_error")

        response_data = cast(Mapping[str, object], response)
        raw_messages = response_data.get("messages")
        if not isinstance(raw_messages, list):
            return CardLookup(CardLookupStatus.INDETERMINATE, reason="invalid_messages")
        for raw_message in cast(list[object], raw_messages):
            message = _as_mapping(raw_message)
            if message is None:
                return CardLookup(CardLookupStatus.INDETERMINATE, reason="invalid_message")
            if _has_cancel_key(message, cancel_key=cancel_key):
                message_ts = message.get("ts")
                if not isinstance(message_ts, str) or not message_ts:
                    return CardLookup(CardLookupStatus.INDETERMINATE, reason="missing_timestamp")
                matches.append(message_ts)

        metadata_value = response_data.get("response_metadata")
        metadata = _as_mapping(metadata_value) if metadata_value is not None else {}
        if metadata is None:
            return CardLookup(CardLookupStatus.INDETERMINATE, reason="invalid_metadata")
        next_cursor = metadata.get("next_cursor")
        if next_cursor is None:
            next_cursor = ""
        if not isinstance(next_cursor, str):
            return CardLookup(CardLookupStatus.INDETERMINATE, reason="invalid_cursor")
        next_cursor = next_cursor.strip()
        if not next_cursor:
            if response_data.get("has_more"):
                return CardLookup(CardLookupStatus.INDETERMINATE, reason="missing_cursor")
            return CardLookup(
                CardLookupStatus.MULTIPLE
                if len(matches) > 1
                else (CardLookupStatus.FOUND if matches else CardLookupStatus.NOT_FOUND),
                message_ts=matches[0] if matches else None,
                message_timestamps=tuple(matches),
                reason="duplicate_key" if len(matches) > 1 else None,
            )
        if next_cursor in seen_cursors:
            return CardLookup(CardLookupStatus.INDETERMINATE, reason="pagination_stalled")
        if len(seen_cursors) + 1 >= _PAGE_BUDGET:
            return CardLookup(CardLookupStatus.INDETERMINATE, reason="scan_limit")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
