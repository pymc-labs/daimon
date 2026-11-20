"""A conversation's earlier messages as the agent reads them: `<context>`, the replayed
messages in an untrusted block, then the `<user_query>`. Adapters convert their native
messages to `HistoryMessage`; eval items render a case's history the same way."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from xml.sax.saxutils import escape, quoteattr

from daimon.core.turn_keys import render_keys_element
from daimon.core.untrusted import untrusted_block


@dataclass(frozen=True)
class HistoryAttachment:
    name: str
    url: str
    media_type: str


@dataclass(frozen=True)
class HistoryMessage:
    user_id: str
    username: str
    is_bot: bool
    timestamp: str
    text: str
    attachments: Sequence[HistoryAttachment] = ()


@dataclass(frozen=True)
class ContextOptions:
    channel_id: str
    block: str = "thread_history"
    """`thread_history`, `thread_delta` or `channel_context`."""
    block_attrs: Mapping[str, str] | None = None
    key_names: Sequence[str] = ()
    is_admin: bool = False
    author_name: str | None = None


def render_message(message: HistoryMessage) -> list[str]:
    """One message as XML lines; attribute values quoted, text escaped."""
    attrs = (
        f" user_id={quoteattr(message.user_id)}"
        f" username={quoteattr(message.username)}"
        f" is_bot={quoteattr('true' if message.is_bot else 'false')}"
        f" timestamp={quoteattr(message.timestamp)}"
    )
    text = escape(message.text)
    if not message.attachments:
        return [f"<message{attrs}>{text}</message>"]
    lines = [f"<message{attrs}>", text, "<attachments>"]
    lines += [
        f"<attachment name={quoteattr(a.name)} url={quoteattr(a.url)}"
        f" mimetype={quoteattr(a.media_type)}/>"
        for a in message.attachments
    ]
    return [*lines, "</attachments>", "</message>"]


def user_query_open_tag(author_id: str, is_admin: bool, author_name: str | None = None) -> str:
    """Opening `<user_query>`: bare without an author, else its quoted id, `is_admin` as
    "true"/"false" and the display name when there is one (user text, read as data)."""
    if not author_id:
        return "<user_query>"
    name_attr = f" author_name={quoteattr(author_name)}" if author_name else ""
    return (
        f"<user_query author_id={quoteattr(author_id)}"
        f" is_admin={quoteattr('true' if is_admin else 'false')}{name_attr}>"
    )


def render_context(
    platform: str,
    history: Sequence[HistoryMessage],
    trigger: HistoryMessage,
    options: ContextOptions,
) -> str:
    """The context block and the request: `trigger` is the message being answered, its
    `user_id` the author (empty: a bare query)."""
    lines = [
        "<context>",
        f"<channel platform={quoteattr(platform)} id={quoteattr(options.channel_id)}/>",
        render_keys_element(options.key_names),
    ]
    lines = [line for line in lines if line]
    body = [line for message in history for line in render_message(message)]
    attrs = options.block_attrs if options.block_attrs is not None else {"source": platform}
    lines.extend(untrusted_block(options.block, body, attrs))
    tag = user_query_open_tag(trigger.user_id, options.is_admin, options.author_name)
    lines += ["</context>", "", f"{tag}{escape(trigger.text)}</user_query>"]
    return "\n".join(lines)
