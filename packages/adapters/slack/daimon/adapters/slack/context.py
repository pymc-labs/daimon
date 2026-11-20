"""XML context builder for Slack thread history replay.

Builds XML context from a Slack thread's message history via
``conversations.replies``, suitable for prepending to a user's message when
responding in an existing thread.

Mirrors ``packages/adapters/discord/daimon/adapters/discord/context.py``:
- ``build_context_xml``: first-turn fetch, one page from the thread root.
- ``build_delta_xml``: continuation fetch, delta since the watermark timestamp.
- ``build_channel_context_xml``: first turn of a top-level mention, the
  channel's messages up to the mention.

Each fetches a single page. Slack clamps apps commercially distributed outside
the Marketplace to 15 messages and one call a minute; an internal install gets
the page it requests. When Slack reports ``has_more`` the block is marked
``truncated="true"`` so the model knows the window is partial.

All message text is escaped via ``xml.sax.saxutils``.
Thread history is the turn's own conversation, so its fetch errors propagate to
the listener boundary. Channel context is optional: a failed fetch renders it
``status="unavailable"`` and the turn goes ahead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import aiohttp
import structlog
from daimon.adapters.slack.attachments import ProxyUrlContext, build_proxy_url
from daimon.adapters.slack.channel_reads import ChannelReadPolicy
from daimon.adapters.slack.vision import SlackFile
from daimon.core.chat_history import (
    ContextOptions,
    HistoryAttachment,
    HistoryMessage,
    render_context,
)
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger(__name__)

# Requested page size, matching the 100 messages Discord replays; Slack may
# grant fewer and report has_more.
DEFAULT_PAGE_LIMIT = 100

# Channel messages fetched for a top-level mention, the trigger included,
# matching Discord's CHANNEL_BACKFILL_LIMIT.
CHANNEL_BACKFILL_LIMIT = 25

# Upper bound on the channel fetch; past it the block is marked unavailable.
CHANNEL_FETCH_TIMEOUT_S = 10.0


def _history_attrs(*, truncated: bool) -> dict[str, str]:
    """Replayed messages come from everyone in the thread, so they ride in the
    shared untrusted envelope; only the `<user_query>` after it is the request."""
    attrs = {"source": "slack"}
    if truncated:
        attrs["truncated"] = "true"
    return attrs


def _replayed(messages: list[dict[str, Any]], *, status_ts: str | None) -> list[dict[str, Any]]:
    """Thread messages minus this turn's own status card.

    The card is posted before history is fetched, so a replay would show the
    model a fresh bot message (`Working on it…`) from the account it answers
    through, which it reads as another agent already handling the request.
    Only the bot message at exactly that ts is dropped: earlier answers from
    the same account stay, since they may belong to a different agent.
    """
    if status_ts is None:
        return messages
    return [m for m in messages if not ("bot_id" in m and m.get("ts") == status_ts)]


def _message_text(msg: dict[str, Any]) -> str:
    """``msg``'s text plus, for a bot post, its notice cards' words.

    Daimon's own notices are attachments with no message text; without them the
    agent would see its setup questions and reminders as empty messages.
    """
    parts = [str(msg.get("text") or "")]
    if "bot_id" in msg:
        attachments = cast(list[Any], msg.get("attachments") or [])
        parts += [
            str(cast(dict[str, Any], a).get("fallback") or "")
            for a in attachments
            if isinstance(a, dict)
        ]
    return "\n".join(part for part in parts if part)


def _history_message(msg: dict[str, Any], *, proxy: ProxyUrlContext | None) -> HistoryMessage:
    """A Slack message dict as a neutral history message. With the proxy configured each
    attached file carries a signed proxy URL the agent can fetch; otherwise files are
    omitted (no fetchable handle available)."""
    files: list[dict[str, Any]] = msg.get("files", []) if proxy is not None else []
    return HistoryMessage(
        user_id=str(msg.get("user", "")),
        username=str(msg.get("username", "") or msg.get("user", "")),
        is_bot="bot_id" in msg,
        timestamp=str(msg.get("ts", "")),
        text=_message_text(msg),
        attachments=tuple(
            HistoryAttachment(
                name=str(f.get("name", "file")),
                url=build_proxy_url(cast("SlackFile", f), proxy),
                media_type=str(f.get("mimetype", "unknown")),
            )
            for f in files
            if proxy is not None
        ),
    )


def _render(
    channel: str,
    messages: Sequence[dict[str, Any]],
    *,
    block: str,
    attrs: dict[str, str],
    user_query: str,
    author_id: str,
    is_admin: bool,
    author_name: str | None,
    proxy: ProxyUrlContext | None,
    key_names: Sequence[str],
) -> str:
    return render_context(
        "slack",
        [_history_message(m, proxy=proxy) for m in messages],
        HistoryMessage(user_id=author_id, username="", is_bot=False, timestamp="", text=user_query),
        ContextOptions(
            channel_id=channel, block=block, block_attrs=attrs, key_names=key_names,
            is_admin=is_admin, author_name=author_name,
        ),
    )  # fmt: skip


async def build_context_xml(
    client: AsyncWebClient,
    *,
    channel: str,
    thread_ts: str,
    user_query: str,
    author_id: str = "",
    is_admin: bool = False,
    author_name: str | None = None,
    proxy: ProxyUrlContext | None = None,
    key_names: Sequence[str] = (),
    page_limit: int = DEFAULT_PAGE_LIMIT,
    status_ts: str | None = None,
) -> str:
    """Build XML context from thread history for the first turn.

    Fetches one page of ``page_limit`` messages via
    ``conversations.replies``. Returns a string with a
    ``<context>/<thread_history>`` block containing the replayed messages
    followed by a ``<user_query>`` element.

    Window: ``conversations.replies`` always returns oldest-first from the
    thread root, and ``oldest``/``latest`` only filter, so the newest window
    cannot be requested in one call. A thread longer than one page therefore
    replays the root and the first replies, and the block carries
    ``truncated="true"`` so the model knows the recent tail is missing.

    ``key_names`` names this agent's stored keys, names only (see
    `daimon.core.turn_keys`); empty renders no ``<keys>`` element at all.

    ``status_ts`` is this turn's own status card, left out of the replay.
    """
    # A chat history limit of 0 reads none; Slack would read 0 as its own default.
    messages: list[dict[str, Any]] = []
    truncated = False
    if page_limit > 0:
        resp = await client.conversations_replies(  # pyright: ignore[reportUnknownMemberType]
            channel=channel, ts=thread_ts, limit=page_limit
        )
        messages = cast(list[dict[str, Any]], resp["messages"])  # pyright: ignore[reportUnknownVariableType]
        truncated = bool(resp.get("has_more"))  # pyright: ignore[reportUnknownMemberType]

    return _render(
        channel, _replayed(messages, status_ts=status_ts), block="thread_history",
        attrs=_history_attrs(truncated=truncated), user_query=user_query, author_id=author_id,
        is_admin=is_admin, author_name=author_name, proxy=proxy, key_names=key_names,
    )  # fmt: skip


async def build_delta_xml(
    client: AsyncWebClient,
    *,
    channel: str,
    thread_ts: str,
    watermark_ts: str,
    user_query: str,
    author_id: str = "",
    is_admin: bool = False,
    author_name: str | None = None,
    proxy: ProxyUrlContext | None = None,
    key_names: Sequence[str] = (),
    page_limit: int = DEFAULT_PAGE_LIMIT,
    status_ts: str | None = None,
) -> str:
    """Build XML context for a continuation turn (delta since watermark).

    Fetches only messages after ``watermark_ts`` via ``conversations.replies``
    with ``oldest=watermark_ts, inclusive=False`` (mirroring Discord
    ``build_delta_xml``'s ``after_message_id`` path).  Returns a string with a
    ``<context>/<thread_delta>`` block and a ``<user_query>`` element.

    One page of ``page_limit`` messages, oldest-first from the
    watermark; a delta longer than that is marked ``truncated="true"``.

    ``key_names`` names this agent's stored keys, names only (see
    `daimon.core.turn_keys`); empty renders no ``<keys>`` element at all.

    ``status_ts`` is this turn's own status card, left out of the replay.
    """
    resp = await client.conversations_replies(  # pyright: ignore[reportUnknownMemberType]
        channel=channel,
        ts=thread_ts,
        oldest=watermark_ts,
        inclusive=False,
        limit=page_limit,
    )
    messages = cast(list[dict[str, Any]], resp["messages"])  # pyright: ignore[reportUnknownVariableType]
    truncated = bool(resp.get("has_more"))  # pyright: ignore[reportUnknownMemberType]

    return _render(
        channel, _replayed(messages, status_ts=status_ts), block="thread_delta",
        attrs=_history_attrs(truncated=truncated), user_query=user_query, author_id=author_id,
        is_admin=is_admin, author_name=author_name, proxy=proxy, key_names=key_names,
    )  # fmt: skip


def _without_rate_limit_wait(client: AsyncWebClient) -> AsyncWebClient:
    """The same client minus its 429 retry.

    That retry waits out ``Retry-After``, a full minute on an install held to
    one history call a minute, before the turn could start.
    """
    return AsyncWebClient(
        token=client.token,
        base_url=client.base_url,
        timeout=client.timeout,
        session=client.session,
        retry_handlers=[
            h for h in client.retry_handlers if not isinstance(h, AsyncRateLimitErrorRetryHandler)
        ],
    )


def _before(message: dict[str, Any], trigger_ts: str) -> bool:
    """Strictly older than the trigger. ``latest`` already ends the page at
    the trigger; this drops the trigger itself and holds the cutoff for any
    message a page returns past it."""
    try:
        return Decimal(str(message.get("ts", ""))) < Decimal(trigger_ts)
    except InvalidOperation:
        return False


async def _channel_history(
    client: AsyncWebClient, *, channel: str, trigger_ts: str, limit: int
) -> tuple[list[dict[str, Any]], bool] | None:
    """One newest-first page ending at the trigger, or None when it can't be had."""
    try:
        async with asyncio.timeout(CHANNEL_FETCH_TIMEOUT_S):
            resp = await _without_rate_limit_wait(client).conversations_history(  # pyright: ignore[reportUnknownMemberType]
                channel=channel, latest=trigger_ts, inclusive=True, limit=limit
            )
    except (SlackApiError, aiohttp.ClientError, TimeoutError) as exc:
        log.warning("slack.channel_context.unavailable", channel_id=channel, error=str(exc))
        return None
    messages = cast(list[dict[str, Any]], resp["messages"])  # pyright: ignore[reportUnknownVariableType]
    return messages, bool(resp.get("has_more"))  # pyright: ignore[reportUnknownMemberType]


async def build_channel_context_xml(
    client: AsyncWebClient,
    *,
    channel: str,
    trigger_ts: str,
    user_query: str,
    read_policy: ChannelReadPolicy | None,
    author_id: str = "",
    is_admin: bool = False,
    author_name: str | None = None,
    proxy: ProxyUrlContext | None = None,
    key_names: Sequence[str] = (),
    limit: int = CHANNEL_BACKFILL_LIMIT,
) -> str:
    """Build XML context from channel history for a top-level mention's first turn.

    One ``conversations.history`` page of ``limit`` messages ending at the
    trigger (``latest=trigger_ts``, inclusive), so nothing posted after the
    mention is shown and a recovery reseed rebuilds the same window. The
    trigger is left out; earlier answers from the bot stay. This turn's
    status card is a reply in the new thread, so channel history never
    returns it. Messages render oldest first in a
    ``<context>/<channel_context>`` block, followed by the ``<user_query>``.

    ``read_policy`` decides what may be shown (`load_channel_read_policy`); None renders
    the block ``status="unavailable"`` without fetching, as does a fetch that
    fails, is rate limited or times out. Messages in a thread whose readers
    are limited on their own are withheld before any attachment URL is
    minted. ``truncated="true"`` says older messages exist beyond the window:
    Slack may return fewer than ``limit`` (15 for apps distributed outside
    the Marketplace).
    """
    history: tuple[list[dict[str, Any]], bool] | None = (
        None
        if read_policy is None
        else ([], False)
        if limit <= 0
        else await _channel_history(client, channel=channel, trigger_ts=trigger_ts, limit=limit)
    )

    if read_policy is None or history is None:
        shown: list[dict[str, Any]] = []
        attrs = {"source": "slack", "status": "unavailable"}
    else:
        messages, has_more = history
        shown = [
            m
            for m in reversed(messages)
            if _before(m, trigger_ts) and read_policy.message_readable(m)
        ]
        attrs = {"source": "slack", "count": str(len(shown))}
        if has_more:
            attrs["truncated"] = "true"
    return _render(
        channel, shown, block="channel_context", attrs=attrs, user_query=user_query,
        author_id=author_id, is_admin=is_admin, author_name=author_name, proxy=proxy,
        key_names=key_names,
    )  # fmt: skip
