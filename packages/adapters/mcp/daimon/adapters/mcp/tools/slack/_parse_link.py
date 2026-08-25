"""Slack permalink parser (pure — no I/O).

Parses a Slack message permalink
(``https://<workspace>.slack.com/archives/<channel>/p<digits>``) into a
channel id and dotted message ts, and — when the link carries
``?thread_ts=...`` (a reply permalink) — the parent thread ts. Mirrors
``tools/discord/_read.py``'s ``_parse_link_impl``: pure function, no
client, no DB, no clock.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from daimon.adapters.mcp.tools.slack._models import SlackParsedLink
from fastmcp.exceptions import ToolError

_HOST_PATTERN = re.compile(r"^[a-z0-9-]+\.slack\.com$", re.IGNORECASE)
_PATH_PATTERN = re.compile(r"^/archives/(?P<channel>[A-Za-z0-9]+)/p(?P<ts>\d+)$")

# A p-value needs at least one digit before the inserted dot, or the
# "seconds" half of the ts would be empty.
_MIN_TS_DIGITS = 7

_NOT_RECOGNIZED = "not a recognized slack message link"


def _dotted_ts(digits: str) -> str:
    return f"{digits[:-6]}.{digits[-6:]}"


def _slack_parse_link_impl(url: str) -> SlackParsedLink:  # pyright: ignore[reportUnusedFunction]
    """Parse a Slack permalink into structured components.

    Pure function — no network calls. Routing hint references
    read_thread/get_message.
    """
    parts = urlsplit(url)
    if not _HOST_PATTERN.match(parts.netloc):
        raise ToolError(_NOT_RECOGNIZED)
    match = _PATH_PATTERN.match(parts.path)
    if not match:
        raise ToolError(_NOT_RECOGNIZED)

    channel_id = match.group("channel")
    digits = match.group("ts")
    if len(digits) < _MIN_TS_DIGITS:
        raise ToolError(_NOT_RECOGNIZED)
    message_ts = _dotted_ts(digits)

    query = parse_qs(parts.query)
    thread_ts_values = query.get("thread_ts")
    thread_ts = thread_ts_values[0] if thread_ts_values else None

    if thread_ts is not None:
        hint = (
            f'try read_thread(thread_id="{channel_id}:{thread_ts}") — '
            "this link is a reply, so the thread id is its parent ts, not "
            "the message's own ts"
        )
    else:
        hint = (
            f'try read_thread(thread_id="{channel_id}:{message_ts}") first — '
            "conversations.replies returns a valid one-message thread even "
            "for a non-reply message; if that fails, it is a plain message: "
            f'get_message(channel_id="{channel_id}", message_id="{message_ts}")'
        )

    return SlackParsedLink(
        channel_id=channel_id,
        message_ts=message_ts,
        thread_ts=thread_ts,
        hint=hint,
    )
