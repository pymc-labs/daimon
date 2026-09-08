"""XML context builder for Slack thread history replay.

Builds XML context from a Slack thread's message history via
``conversations.replies``, suitable for prepending to a user's message when
responding in an existing thread.

Mirrors ``packages/adapters/discord/daimon/adapters/discord/context.py``:
- ``build_context_xml``: first-turn fetch, one page from the thread root.
- ``build_delta_xml``: continuation fetch, delta since the watermark timestamp.

Both fetch a single page. A workspace whose app is commercially distributed
outside the Marketplace answers with at most 15 objects and allows one call per
minute, so paginating inside a turn is not viable there; when Slack reports
``has_more`` the block is marked ``truncated="true"`` so the model knows the
window is partial.

All message text is escaped via ``xml.sax.saxutils`` (T-80-XML mitigation).
No try/except — exceptions propagate to the listener boundary.
"""

from __future__ import annotations

from typing import Any, cast
from xml.sax.saxutils import escape, quoteattr

from daimon.adapters.slack.attachments import ProxyUrlContext, build_proxy_url
from daimon.adapters.slack.vision import SlackFile
from slack_sdk.web.async_client import AsyncWebClient

# Messages requested per ``conversations.replies`` call. Slack clamps ``limit``
# server-side rather than erroring: a workspace whose app is commercially
# distributed outside the Marketplace returns 15 however much we ask for, an
# internal-app install returns the full page. Asking for the real page size is
# what lets the second class replay a deep thread; ``has_more`` reports which
# class answered.
DEFAULT_PAGE_LIMIT = 200


def _open_tag(name: str, *, truncated: bool) -> str:
    return f'<{name} truncated="true">' if truncated else f"<{name}>"


def _render_message(msg: dict[str, Any], *, proxy: ProxyUrlContext | None) -> list[str]:
    """Render a single Slack message dict as XML lines.

    When the proxy is configured, each attached file becomes an
    ``<attachment>`` element whose ``url`` is a signed proxy URL the agent can
    fetch; otherwise files are omitted (no fetchable handle available).
    Attribute values are XML-quoted; text content is XML-escaped.
    """
    user_id = str(msg.get("user", ""))
    username = str(msg.get("username", "") or msg.get("user", ""))
    ts = str(msg.get("ts", ""))
    is_bot = "true" if "bot_id" in msg else "false"
    text = escape(str(msg.get("text", "")))

    attrs = (
        f" user_id={quoteattr(user_id)}"
        f" username={quoteattr(username)}"
        f" is_bot={quoteattr(is_bot)}"
        f" timestamp={quoteattr(ts)}"
    )

    files: list[dict[str, Any]] = msg.get("files", []) if proxy is not None else []
    if proxy is None or not files:
        return [f"<message{attrs}>{text}</message>"]

    lines = [f"<message{attrs}>", text, "<attachments>"]
    for f in files:
        proxy_url = build_proxy_url(cast("SlackFile", f), proxy)
        att_attrs = (
            f" name={quoteattr(str(f.get('name', 'file')))}"
            f" url={quoteattr(proxy_url)}"
            f" mimetype={quoteattr(str(f.get('mimetype', 'unknown')))}"
        )
        lines.append(f"<attachment{att_attrs}/>")
    lines.append("</attachments>")
    lines.append("</message>")
    return lines


def _user_query_open_tag(author_id: str) -> str:
    """Opening <user_query> tag, with a quoted author_id attribute when present."""
    if not author_id:
        return "<user_query>"
    return f"<user_query author_id={quoteattr(author_id)}>"


async def build_context_xml(
    client: AsyncWebClient,
    *,
    channel: str,
    thread_ts: str,
    user_query: str,
    author_id: str = "",
    proxy: ProxyUrlContext | None = None,
    page_limit: int = DEFAULT_PAGE_LIMIT,
) -> str:
    """Build XML context from thread history for the first turn.

    Fetches one page of ``page_limit`` messages via
    ``conversations.replies``. Returns a string with a
    ``<context>/<thread_history>`` block containing the replayed messages
    followed by a ``<user_query>`` element.

    Window: ``conversations.replies`` always returns oldest-first from the
    thread root, and ``oldest``/``latest`` only filter, so the newest window
    cannot be requested in one call. A thread longer than the page the
    workspace grants therefore replays the root and the first replies, and the
    block carries ``truncated="true"`` so the model knows the recent tail is
    missing.
    """
    resp = await client.conversations_replies(  # pyright: ignore[reportUnknownMemberType]
        channel=channel, ts=thread_ts, limit=page_limit
    )
    messages = cast(list[dict[str, Any]], resp["messages"])  # pyright: ignore[reportUnknownVariableType]
    truncated = bool(resp.get("has_more"))  # pyright: ignore[reportUnknownMemberType]

    lines: list[str] = [
        "<context>",
        f"<channel platform={quoteattr('slack')} id={quoteattr(channel)}/>",
        _open_tag("thread_history", truncated=truncated),
    ]
    for msg in messages:
        lines.extend(_render_message(msg, proxy=proxy))
    lines.append("</thread_history>")
    lines.append("</context>")
    lines.append("")
    lines.append(f"{_user_query_open_tag(author_id)}{escape(user_query)}</user_query>")

    return "\n".join(lines)


async def build_delta_xml(
    client: AsyncWebClient,
    *,
    channel: str,
    thread_ts: str,
    watermark_ts: str,
    user_query: str,
    author_id: str = "",
    proxy: ProxyUrlContext | None = None,
    page_limit: int = DEFAULT_PAGE_LIMIT,
) -> str:
    """Build XML context for a continuation turn (delta since watermark).

    Fetches only messages after ``watermark_ts`` via ``conversations.replies``
    with ``oldest=watermark_ts, inclusive=False`` (mirroring Discord
    ``build_delta_xml``'s ``after_message_id`` path).  Returns a string with a
    ``<context>/<thread_delta>`` block and a ``<user_query>`` element.

    One page of ``page_limit`` messages, oldest-first from the watermark; a
    delta longer than that is marked ``truncated="true"``.
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

    lines: list[str] = [
        "<context>",
        f"<channel platform={quoteattr('slack')} id={quoteattr(channel)}/>",
        _open_tag("thread_delta", truncated=truncated),
    ]
    for msg in messages:
        lines.extend(_render_message(msg, proxy=proxy))
    lines.append("</thread_delta>")
    lines.append("</context>")
    lines.append("")
    lines.append(f"{_user_query_open_tag(author_id)}{escape(user_query)}</user_query>")

    return "\n".join(lines)
