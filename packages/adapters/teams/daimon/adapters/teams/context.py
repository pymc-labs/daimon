"""Turn context XML for Teams: thread history from Graph, then the person's words. Pure.

Mirrors the Discord and Slack `context.py`: a first turn replays the thread
(root plus the newest replies), a continuation only what came after the
session watermark, and a top-level mention the channel's recent posts with
their newest replies. The newest replayed images are inlined and shared files
in the channel's Files folder get a download URL. Every replayed message
rides in the shared untrusted envelope, every value escaped.
Graph bodies are HTML; `teams_graph.html_to_text` keeps the words, names `<at>` mentions
and marks images. System events, deleted posts and the bot's own cards (the
status card, notices) are dropped; its answers, plain messages, stay. Each
replayed message carries its `message_id`, so the agent can name its own to
edit_message, delete_message or delete_reply. A 1:1 chat replays nothing, so
its turn lists the agent's own recent posts there instead, by reply (`own_posts`).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import httpx
from daimon.adapters.teams.attachments import ChannelMedia, SharedFile
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.chat_history import (
    ContextOptions,
    HistoryAttachment,
    HistoryMessage,
    render_frame,
    render_message,
    teams_head,
    teams_history_unavailable,
    teams_own_posts,
)
from daimon.core.teams_graph import (
    FILE_ATTACHMENT_TYPES,
    GraphMessage,
    GraphPage,
    image_sources,
    is_graph_url,
    linked_files,
    message_text,
)
from daimon.core.teams_sharepoint import file_link
from daimon.core.thread_participation import ClassifierMessage

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
    id), and download URLs and versions (eTags) of shared files (by SharePoint URL)."""

    images: Mapping[str, int] = field(default_factory=dict[str, int])
    downloads: Mapping[str, str] = field(default_factory=dict[str, str])
    versions: Mapping[str, str] = field(default_factory=dict[str, str])


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
    """Files shared in the message, and the SharePoint files a bot's message links to.

    A link resolves only inside the channel's folder, like any shared file, so
    the agent can open what it delivered earlier.
    """
    shared = [
        SharedFile(a.name or "file", a.content_url)
        for a in message.attachments
        if a.content_type in FILE_ATTACHMENT_TYPES
    ]
    return shared + [SharedFile(name, url) for name, url in linked_files(message)]


# Headed daimon's file links until the list stood alone; older answers still carry it.
_LEGACY_HEADING = "Saved to this channel's files:"


def _without_saved_links(text: str, message: GraphMessage) -> str:
    """daimon's answer without the file links it added below: replayed, the agent copied them.

    They end the answer, one line per link, among blank lines and its previews'
    `[image]`. Older answers head them with `_LEGACY_HEADING`, unlinked lines included.
    """
    lines = text.split("\n")
    stripped = [line.strip() for line in lines]
    dropped: set[int] = set()
    if _LEGACY_HEADING in stripped:
        start = len(stripped) - 1 - stripped[::-1].index(_LEGACY_HEADING)
        tail = range(start + 1, len(lines))
        if all(stripped[n] in ("", "[image]") or stripped[n].startswith("- ") for n in tail):
            dropped = {start, *(n for n in tail if stripped[n].startswith("- "))}
    else:
        links = [{f"- {name}", file_link(name, url)} for name, url in linked_files(message)]
        index = len(lines)
        while index and links:
            index -= 1
            if stripped[index] in ("", "[image]"):
                continue
            if (link := next((ln for ln in links if stripped[index] in ln), None)) is None:
                break
            links.remove(link)
            dropped.add(index)
    kept = [line for n, line in enumerate(lines) if n not in dropped]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def channel_media(
    message: GraphMessage, *, group_id: str, channel_id: str, root_id: str
) -> ChannelMedia:
    """The message's own hosted images (on Graph) and its shared files.

    An `<img>` is fetched with the app's token, so only this message's hosted
    content qualifies, never another team's or message's.
    """
    prefix = f"/v1.0/teams/{group_id}/channels/{channel_id}/messages/{root_id}"
    if message.id != root_id:
        prefix += f"/replies/{message.id}"
    return _media(message, prefix)


def chat_media(message: GraphMessage, *, chat_id: str) -> ChannelMedia:
    """A group chat message's own hosted images and shared files, as `channel_media`."""
    return _media(message, f"/v1.0/chats/{chat_id}/messages/{message.id}")


def _media(message: GraphMessage, prefix: str) -> ChannelMedia:
    images: tuple[str, ...] = ()
    if message.body.content_type == "html":
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
    extra: Mapping[str, str] | None = None,
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
    is_self = is_bot and app is not None and app.id == bot_app_id
    if is_self:
        # Its files are listed as attachments below, not in its words.
        text = _without_saved_links(text, message)
    extra_attrs = dict(extra or {})
    if images := attached.images.get(message.id):
        extra_attrs["images_attached"] = str(images)
    return render_message(
        HistoryMessage(
            user_id=(author.id if author else None) or "",
            username=(author.display_name if author else None) or "unknown",
            is_bot=is_bot,
            timestamp=message.created_date_time or "",
            text=text,
            attachments=[_attachment(f, attached) for f in files],
            message_id=message.id,
            is_self=is_self,
            extra_attrs=extra_attrs,
        ),
        "teams",
    )


def _attachment(file: SharedFile, attached: Attached) -> HistoryAttachment:
    key = file.content_url or ""
    return HistoryAttachment(
        name=file.name,
        url=attached.downloads.get(key) or None,
        link=file.content_url,
        version=attached.versions.get(key),
    )


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


def chat_messages(page: GraphPage, *, skip_ids: frozenset[str]) -> list[GraphMessage]:
    """What a group chat's first turn replays, oldest first: its newest messages."""
    return [m for m in _order(page.value) if m.id not in skip_ids and _readable(m)]


def chat_block(
    page: GraphPage,
    *,
    skip_ids: frozenset[str],
    bot_app_id: str,
    attached: Attached = NOTHING_ATTACHED,
) -> HistoryBlock:
    """A group chat's first turn: one page of its newest messages."""
    messages = chat_messages(page, skip_ids=skip_ids)
    lines = _lines(messages, bot_app_id=bot_app_id, attached=attached)
    newest = newest_message_id(m.id for m in page.value)
    truncated = _truncated(page.next_link is not None)
    return HistoryBlock("chat_history", lines, truncated, newest, attached)


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
        thread = {"thread_id": f"{channel_id};messageid={post.id}"}
        for message in (post, *replies):
            if message.id in skip_ids:
                continue
            extra = thread | ({"more_replies": "true"} if message is post and more else {})
            rendered = _render(message, bot_app_id=bot_app_id, attached=attached, extra=extra)
            count += bool(rendered)
            lines += rendered
    return HistoryBlock("channel_context", tuple(lines), {"count": str(count)}, attached=attached)


def history_media(
    messages: Sequence[GraphMessage],
    *,
    group_id: str = "",
    channel_id: str = "",
    chat_id: str | None = None,
) -> tuple[dict[str, tuple[str, ...]], list[SharedFile]]:
    """The newest replayed images (by message id) and shared files, within the limits.

    `chat_id` reads them as a group chat's messages instead of a channel's.
    """
    images: dict[str, tuple[str, ...]] = {}
    files: list[SharedFile] = []
    room = HISTORY_IMAGE_LIMIT
    for message in reversed(messages):
        media = (
            chat_media(message, chat_id=chat_id)
            if chat_id is not None
            else channel_media(
                message, group_id=group_id, channel_id=channel_id, root_id=root_of(message)
            )
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
    own_posts: Sequence[Sequence[tuple[str, str | None, str]]] = (),
) -> str:
    """Host facts and any replayed history, then the person's escaped words.

    A group chat is one `role="group_chat"` place without files. A channel turn
    names its thread too, the id thread-scoped settings such as
    `change_setting` key on, and says with `files` whether a file
    saved now reaches the channel (`channel_files`; None in a chat). Where it
    does not, `files_hint` says an admin must grant the permission. In a 1:1
    chat `files_hint` says how to picture a document inline.
    `unprompted="true"` marks a message nobody addressed to the bot (organic
    thread participation). `own_posts` are the agent's latest replies in a
    1:1 chat, newest first, each its messages (message id, role, posted at).
    """
    head = [
        *teams_head(
            inbound.kind,
            inbound.channel_id,
            thread_id=inbound.thread_id,
            channel_name=inbound.channel_name,
            channel_type=inbound.channel_type,
            team_name=inbound.team_name,
            channel_files=channel_files,
            is_foreign=inbound.is_foreign,
        ),
        *teams_own_posts(own_posts),
        *([keys] if keys else []),
    ]
    options = ContextOptions(
        channel_id=inbound.channel_id,
        block=None,
        is_admin=is_admin,
        head=head,
        unprompted=inbound.unprompted,
        lead=controls,
        query_prefix=prefix,
    )
    body: Sequence[str] = ()
    if history is not None and history.unavailable:
        options = dataclasses.replace(
            options, tail=(teams_history_unavailable(inbound.kind, history.unavailable),)
        )
    elif history is not None:
        options = dataclasses.replace(
            options, block=history.tag, block_attrs={"source": "teams", **history.attrs}
        )
        body = history.lines
    trigger = HistoryMessage(
        user_id=inbound.user_id,
        username=inbound.user_name or "",
        is_bot=False,
        timestamp=inbound.timestamp or "",
        text=inbound.text,
    )
    return render_frame("teams", body, trigger, options)
