"""Turn context XML for Teams: thread history from Graph, then the person's words. Pure.

Mirrors the Discord and Slack `context.py`: a first turn replays the thread
(root plus the newest replies), a continuation only what came after the
session watermark, and a top-level mention the channel's recent posts with
their newest replies. The newest replayed images are inlined and shared files
get a download URL when the team's site is granted. Every replayed message
rides in the shared untrusted envelope, every value escaped.
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
# Replies replayed under each of those posts, the newest kept.
CHANNEL_REPLIES_PER_POST = 10
# Per turn: earlier messages' images inlined and files given a download URL, newest first.
HISTORY_IMAGE_LIMIT = 4
HISTORY_FILE_LIMIT = 10


@dataclass(frozen=True)
class Attached:
    """What replayed messages carry beyond text: the images inlined (count by message
    id) and download URLs of shared files (by SharePoint URL)."""

    images: Mapping[str, int] = field(default_factory=dict[str, int])
    downloads: Mapping[str, str] = field(default_factory=dict[str, str])


NOTHING_ATTACHED = Attached()


@dataclass(frozen=True)
class HistoryBlock:
    """Rendered `<message>` lines for one envelope, `tag` naming the window.

    `newest_id` is the newest message read in the thread, rendered or not.
    `image_urls` are the hosted images `attached.images` counts, in history
    order. `unavailable` says why Graph could not be read; such a block has
    no lines.
    """

    tag: str
    lines: tuple[str, ...]
    attrs: Mapping[str, str] = field(default_factory=dict[str, str])
    newest_id: str | None = None
    attached: Attached = NOTHING_ATTACHED
    image_urls: tuple[str, ...] = ()
    unavailable: str | None = None


def unavailable_block(reason: str) -> HistoryBlock:
    return HistoryBlock("history", (), unavailable=reason)


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


def root_of(message: GraphMessage) -> str:
    """The id of the post a message is in: its own for a post."""
    return message.reply_to_id or message.id


def _readable(message: GraphMessage) -> bool:
    if message.message_type != "message" or message.deleted_date_time is not None:
        return False
    # Not a card (the bot's own status or notice) or an empty post.
    return bool(message_text(message) or _files(message))


def _render(
    message: GraphMessage,
    *,
    bot_app_id: str,
    attached: Attached = NOTHING_ATTACHED,
    extra: str = "",
) -> list[str]:
    """One message as `<message>` lines, or none for what a reader would not see as one."""
    if not _readable(message):
        return []
    sender = message.sender
    user = sender.user if sender is not None else None
    app = sender.application if sender is not None else None
    author = user or app
    text, files = message_text(message), _files(message)
    is_bot = user is None and app is not None
    attrs = (
        f" author_name={quoteattr((author.display_name if author else None) or 'unknown')}"
        f" user_id={quoteattr((author.id if author else None) or '')}"
        f" is_bot={quoteattr(str(is_bot).lower())}"
        + (' is_self="true"' if is_bot and app is not None and app.id == bot_app_id else "")
        + f" timestamp={quoteattr(message.created_date_time or '')}"
        + extra
    )
    if images := attached.images.get(message.id):
        attrs += f' images_attached="{images}"'
    if not files:
        return [f"<message{attrs}>{escape(text)}</message>"]
    shared = [_attachment(f, attached.downloads) for f in files]
    return [
        f"<message{attrs}>",
        escape(text),
        "<attachments>",
        *shared,
        "</attachments>",
        "</message>",
    ]


def _attachment(file: SharedFile, downloads: Mapping[str, str]) -> str:
    name = quoteattr(file.name)
    if url := downloads.get(file.content_url or ""):
        hint = quoteattr("short-lived download URL: fetch it now (curl to disk, then read it)")
        return f"<attachment name={name} url={quoteattr(url)} hint={hint}/>"
    return f'<attachment name={name} fetchable="false"/>'


def _order(messages: Iterable[GraphMessage]) -> list[GraphMessage]:
    # Teams message ids are epoch milliseconds, so they sort as posting order.
    return sorted(messages, key=lambda m: int(m.id) if m.id.isdigit() else 0)


def _lines(
    messages: Iterable[GraphMessage], *, bot_app_id: str, attached: Attached
) -> tuple[str, ...]:
    return tuple(
        line for m in messages for line in _render(m, bot_app_id=bot_app_id, attached=attached)
    )


def _truncated(is_truncated: bool) -> dict[str, str]:
    return {"truncated": "true"} if is_truncated else {}


def thread_messages(
    root: GraphMessage, replies: GraphPage, *, skip_ids: frozenset[str]
) -> list[GraphMessage]:
    """What a first turn replays, oldest first: the root and one page of newest replies."""
    return [m for m in _order([root, *replies.value]) if m.id not in skip_ids and _readable(m)]


def thread_block(
    root: GraphMessage,
    replies: GraphPage,
    *,
    skip_ids: frozenset[str],
    bot_app_id: str,
    attached: Attached = NOTHING_ATTACHED,
) -> HistoryBlock:
    """First turn in a thread: the root and one page of its newest replies."""
    messages = thread_messages(root, replies, skip_ids=skip_ids)
    lines = _lines(messages, bot_app_id=bot_app_id, attached=attached)
    newest = newest_message_id(m.id for m in [root, *replies.value])
    truncated = _truncated(replies.next_link is not None)
    return HistoryBlock("thread_history", lines, truncated, newest, attached)


def delta_messages(
    replies: GraphPage, *, after: int, skip_ids: frozenset[str]
) -> list[GraphMessage]:
    """What a continuation replays, oldest first: replies newer than the watermark."""
    newer = (m for m in replies.value if m.id.isdigit() and int(m.id) > after)
    return [m for m in _order(newer) if m.id not in skip_ids and _readable(m)]


def delta_block(
    replies: GraphPage,
    *,
    after: int,
    skip_ids: frozenset[str],
    bot_app_id: str,
    attached: Attached = NOTHING_ATTACHED,
) -> HistoryBlock:
    """A continuation: replies newer than the watermark `after`.

    The page is newest first, so it is cut short only when every reply on it is
    still newer than the watermark and Graph has more.
    """
    newer = [m for m in replies.value if m.id.isdigit() and int(m.id) > after]
    cut = replies.next_link is not None and len(newer) == len(replies.value)
    messages = delta_messages(replies, after=after, skip_ids=skip_ids)
    lines = _lines(messages, bot_app_id=bot_app_id, attached=attached)
    newest = newest_message_id(m.id for m in replies.value)
    return HistoryBlock("thread_delta", lines, _truncated(cut), newest, attached)


def _replies_kept(post: GraphMessage) -> tuple[list[GraphMessage], bool]:
    """The post's newest replies, oldest first, and whether any were left out."""
    replies = _order(post.replies)
    kept = replies[-CHANNEL_REPLIES_PER_POST:]
    return kept, len(kept) < len(replies) or post.replies_next_link is not None


def channel_messages(posts: GraphPage, *, skip_ids: frozenset[str]) -> list[GraphMessage]:
    """What a top-level mention replays: each post, oldest first, then its newest replies."""
    kept: list[GraphMessage] = []
    for post in _order(posts.value):
        replies, _ = _replies_kept(post)
        kept += [m for m in (post, *replies) if m.id not in skip_ids and _readable(m)]
    return kept


def channel_block(
    posts: GraphPage,
    *,
    skip_ids: frozenset[str],
    bot_app_id: str,
    channel_id: str,
    attached: Attached = NOTHING_ATTACHED,
) -> HistoryBlock:
    """A top-level mention: the channel's most recently active posts and their replies.

    Each message names its thread, so the agent can read a whole one with read_thread.
    """
    lines: list[str] = []
    count = 0
    for post in _order(posts.value):
        replies, more = _replies_kept(post)
        thread = f" thread_id={quoteattr(f'{channel_id};messageid={post.id}')}"
        for message in (post, *replies):
            if message.id in skip_ids:
                continue
            extra = thread + (' more_replies="true"' if message is post and more else "")
            rendered = _render(message, bot_app_id=bot_app_id, attached=attached, extra=extra)
            count += bool(rendered)
            lines += rendered
    return HistoryBlock("channel_context", tuple(lines), {"count": str(count)}, attached=attached)


def history_media(
    messages: Sequence[GraphMessage], *, group_id: str, channel_id: str
) -> tuple[dict[str, tuple[str, ...]], list[SharedFile]]:
    """The newest replayed images (by message id) and shared files, within the limits."""
    images: dict[str, tuple[str, ...]] = {}
    files: list[SharedFile] = []
    room = HISTORY_IMAGE_LIMIT
    for message in reversed(messages):
        media = channel_media(
            message, group_id=group_id, channel_id=channel_id, root_id=root_of(message)
        )
        if room and media.image_urls:
            images[message.id] = media.image_urls[:room]
            room -= len(images[message.id])
        files += [f for f in media.files if f.content_url]
    return images, files[:HISTORY_FILE_LIMIT]


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
        names = "".join(
            f" {attr}={quoteattr(value)}"
            for attr, value in (
                ("name", inbound.channel_name),
                ("type", inbound.channel_type),
                ("team_name", inbound.team_name),
            )
            if value
        )
        context = [
            f'<channel platform="teams" id={quoteattr(inbound.channel_id)}'
            f' role="parent_channel"{names}{files}/>',
            f'<thread platform="teams" id={quoteattr(inbound.thread_id)} role="current_thread"/>',
        ]
    author = f" author_name={quoteattr(inbound.user_name)}" if inbound.user_name else ""
    sent = f" timestamp={quoteattr(inbound.timestamp)}" if inbound.timestamp else ""
    query = (
        f"<user_query{author} author_id={quoteattr(inbound.user_id)}{sent} "
        f'is_admin="{str(is_admin).lower()}"'
        + (' unprompted="true"' if inbound.unprompted else "")
        + f">{escape(inbound.text)}</user_query>"
    )
    replay: Sequence[str] = ()
    if history is not None and history.unavailable:
        hint = (
            "daimon could not read this conversation's earlier messages from Microsoft Teams. "
            "If the request needs them, say so instead of guessing; read_channel or "
            "read_thread may still reach them."
        )
        reason = quoteattr(history.unavailable)
        replay = (f'<history status="unavailable" reason={reason} hint={quoteattr(hint)}/>',)
    elif history is not None:
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
