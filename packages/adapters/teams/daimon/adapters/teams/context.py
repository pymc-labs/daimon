"""Turn context XML for Teams: thread history from Graph, then the person's words. Pure.

Mirrors the Discord and Slack `context.py`: a first turn replays the thread
(root plus the newest replies), a continuation only what came after the
session watermark, and a top-level mention the channel's recent posts. Every
replayed message rides in the shared untrusted envelope, every value escaped.
Graph bodies are HTML; `teams_graph.html_to_text` keeps the words, names `<at>` mentions
and marks images. System events, deleted posts and the bot's own cards (the
status card, notices) are dropped; its answers, plain messages, stay.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

import httpx
from daimon.adapters.teams.attachments import ChannelMedia, SharedFile
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.teams_graph import (
    FILE_ATTACHMENT_TYPES,
    GraphMessage,
    GraphPage,
    image_sources,
    is_graph_url,
    message_text,
)
from daimon.core.thread_participation import ClassifierMessage
from daimon.core.untrusted import untrusted_block

# Top-level posts replayed for a mention that starts a thread, as on Discord.
CHANNEL_BACKFILL_LIMIT = 25


@dataclass(frozen=True)
class HistoryBlock:
    """Rendered `<message>` lines for one envelope, `tag` naming the window.

    `newest_id` is the newest message read in the thread, rendered or not.
    """

    tag: str
    lines: tuple[str, ...]
    attrs: Mapping[str, str] = field(default_factory=dict[str, str])
    newest_id: str | None = None


def newest_message_id(ids: Iterable[str | None]) -> str | None:
    """The newest of `ids` (Teams message ids are epoch milliseconds), or None."""
    numeric = [int(i) for i in ids if i and i.isdigit()]
    return str(max(numeric)) if numeric else None


def _files(message: GraphMessage) -> list[SharedFile]:
    return [
        SharedFile(a.name or "file", a.content_url)
        for a in message.attachments
        if a.content_type in FILE_ATTACHMENT_TYPES
    ]


def channel_media(
    message: GraphMessage, *, group_id: str, channel_id: str, root_id: str
) -> ChannelMedia:
    """The message's own hosted images (on Graph) and its shared files.

    An `<img>` is fetched with the app's token, so only this message's hosted
    content qualifies, never another team's or message's.
    """
    images: tuple[str, ...] = ()
    if message.body.content_type == "html":
        prefix = f"/v1.0/teams/{group_id}/channels/{channel_id}/messages/{root_id}"
        if message.id != root_id:
            prefix += f"/replies/{message.id}"
        hosted = (src for src in image_sources(message.body.content or ""))
        images = tuple(
            src for src in hosted if _is_hosted_content(src, f"{prefix}/hostedContents/")
        )
    return ChannelMedia(image_urls=images, files=tuple(_files(message)))


def _is_hosted_content(src: str, prefix: str) -> bool:
    try:
        url = httpx.URL(src)
    except httpx.InvalidURL:
        return False
    path = url.path  # decoded, as the ids in `prefix` are
    if not is_graph_url(url) or not path.casefold().startswith(prefix.casefold()):
        return False
    content_id, _, tail = path[len(prefix) :].partition("/")
    return bool(content_id) and tail == "$value"


def _render(message: GraphMessage, *, bot_app_id: str) -> list[str]:
    """One message as `<message>` lines, or none for what a reader would not see as one."""
    if message.message_type != "message" or message.deleted_date_time is not None:
        return []
    sender = message.sender
    user = sender.user if sender is not None else None
    app = sender.application if sender is not None else None
    author = user or app
    text, files = message_text(message), _files(message)
    if not text and not files:
        return []  # a card (the bot's own status or notice) or an empty post
    is_bot = user is None and app is not None
    attrs = (
        f" author_name={quoteattr((author.display_name if author else None) or 'unknown')}"
        f" user_id={quoteattr((author.id if author else None) or '')}"
        f" is_bot={quoteattr(str(is_bot).lower())}"
        + (' is_self="true"' if is_bot and app is not None and app.id == bot_app_id else "")
        + f" timestamp={quoteattr(message.created_date_time or '')}"
    )
    if not files:
        return [f"<message{attrs}>{escape(text)}</message>"]
    # Names only: only the mentioned message's files are looked up in SharePoint.
    shared = [f'<attachment name={quoteattr(f.name)} fetchable="false"/>' for f in files]
    return [
        f"<message{attrs}>",
        escape(text),
        "<attachments>",
        *shared,
        "</attachments>",
        "</message>",
    ]


def _order(messages: Iterable[GraphMessage]) -> list[GraphMessage]:
    # Teams message ids are epoch milliseconds, so they sort as posting order.
    return sorted(messages, key=lambda m: int(m.id) if m.id.isdigit() else 0)


def _rendered(
    messages: Iterable[GraphMessage], *, skip_ids: frozenset[str], bot_app_id: str
) -> list[list[str]]:
    kept = (m for m in _order(messages) if m.id not in skip_ids)
    return [lines for m in kept if (lines := _render(m, bot_app_id=bot_app_id))]


def _lines(
    messages: Iterable[GraphMessage], *, skip_ids: frozenset[str], bot_app_id: str
) -> tuple[str, ...]:
    rendered = _rendered(messages, skip_ids=skip_ids, bot_app_id=bot_app_id)
    return tuple(line for lines in rendered for line in lines)


def _truncated(is_truncated: bool) -> dict[str, str]:
    return {"truncated": "true"} if is_truncated else {}


def thread_block(
    root: GraphMessage, replies: GraphPage, *, skip_ids: frozenset[str], bot_app_id: str
) -> HistoryBlock:
    """First turn in a thread: the root and one page of its newest replies."""
    messages = [root, *replies.value]
    lines = _lines(messages, skip_ids=skip_ids, bot_app_id=bot_app_id)
    newest = newest_message_id(m.id for m in messages)
    return HistoryBlock("thread_history", lines, _truncated(replies.next_link is not None), newest)


def delta_block(
    replies: GraphPage, *, after: int, skip_ids: frozenset[str], bot_app_id: str
) -> HistoryBlock:
    """A continuation: replies newer than the watermark `after`.

    The page is newest first, so it is cut short only when every reply on it is
    still newer than the watermark and Graph has more.
    """
    newer = [m for m in replies.value if m.id.isdigit() and int(m.id) > after]
    cut = replies.next_link is not None and len(newer) == len(replies.value)
    lines = _lines(newer, skip_ids=skip_ids, bot_app_id=bot_app_id)
    newest = newest_message_id(m.id for m in replies.value)
    return HistoryBlock("thread_delta", lines, _truncated(cut), newest)


def classifier_window(
    messages: Iterable[GraphMessage], *, exclude_ids: frozenset[str], limit: int, bot_app_id: str
) -> list[ClassifierMessage]:
    """The `limit` newest readable messages outside the burst, oldest first, for the classifier.

    Only the bot's own messages count as the bot (`is_bot`), as on Discord;
    another app reads as one more participant.
    """
    window: list[ClassifierMessage] = []
    for message in _order(messages):
        if message.id in exclude_ids or message.message_type != "message":
            continue
        if message.deleted_date_time is not None or not (text := message_text(message)):
            continue
        sender = message.sender
        user = sender.user if sender is not None else None
        app = sender.application if sender is not None else None
        author = user or app
        is_self = user is None and app is not None and app.id == bot_app_id
        name = (author.display_name if author else None) or "unknown"
        window.append(ClassifierMessage(author_name=name, content=text, is_bot=is_self))
    return window[-limit:]


def channel_block(posts: GraphPage, *, skip_ids: frozenset[str], bot_app_id: str) -> HistoryBlock:
    """A top-level mention: the channel's most recently active posts."""
    rendered = _rendered(posts.value, skip_ids=skip_ids, bot_app_id=bot_app_id)
    lines = tuple(line for each in rendered for line in each)
    return HistoryBlock("channel_context", lines, {"count": str(len(rendered))})


def render_user_message(
    controls: str,
    inbound: TeamsInbound,
    *,
    is_admin: bool,
    keys: str,
    prefix: str,
    history: HistoryBlock | None,
    channel_files: bool | None = None,
) -> str:
    """Host facts and any replayed history, then the person's escaped words.

    A channel turn names its thread too, the id thread-scoped settings such as
    `set_thread_participation` key on, and says with `files` whether a file
    saved now reaches the channel (`channel_files`; None in a chat).
    `unprompted="true"` marks a message nobody addressed to the bot (organic
    thread participation).
    """
    context = [f'<channel platform="teams" id={quoteattr(inbound.channel_id)}/>']
    if inbound.kind == "channel":
        files = ""
        if channel_files is not None:
            files = f' files="{"available" if channel_files else "unavailable"}"'
        context = [
            f'<channel platform="teams" id={quoteattr(inbound.channel_id)}'
            f' role="parent_channel"{files}/>',
            f'<thread platform="teams" id={quoteattr(inbound.thread_id)} role="current_thread"/>',
        ]
    query = (
        f"<user_query author_id={quoteattr(inbound.user_id)} "
        f'is_admin="{str(is_admin).lower()}"'
        + (' unprompted="true"' if inbound.unprompted else "")
        + f">{escape(inbound.text)}</user_query>"
    )
    replay: Sequence[str] = ()
    if history is not None:
        replay = untrusted_block(history.tag, history.lines, {"source": "teams", **history.attrs})
    return "\n".join(
        [
            controls,
            "<context>",
            *context,
            *([keys] if keys else []),
            *replay,
            "</context>",
            "",
            prefix + query,
        ]
    )
