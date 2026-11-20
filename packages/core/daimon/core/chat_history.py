"""A conversation's earlier messages as the agent reads them: `<context>`, the replayed
messages in an untrusted block, then the `<user_query>`, in each platform's own shape.
Adapters convert their native messages to `HistoryMessage`; eval items render a case's
history the same way."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

from daimon.core.media.vision import MAX_VISION_IMAGE_DIMENSION
from daimon.core.turn_keys import render_keys_element
from daimon.core.untrusted import untrusted_block

#: Platforms whose context has a shape of its own; any other reads like Slack's.
DISCORD, TEAMS = "discord", "teams"


@dataclass(frozen=True)
class HistoryAttachment:
    """A file on a message. `url` None: withheld (Discord) or not fetchable (Teams)."""

    name: str
    url: str | None
    media_type: str = "unknown"
    size: int | None = None
    width: int | None = None
    height: int | None = None
    oversized: bool = False
    """An image past the API's pixel cap: marked, and its URL withheld on a rebuild."""
    link: str | None = None
    version: str | None = None
    """A Teams shared file's SharePoint link and version, to save an edit over it."""


@dataclass(frozen=True)
class HistoryMessage:
    user_id: str
    username: str
    is_bot: bool
    timestamp: str
    text: str
    attachments: Sequence[HistoryAttachment] = ()
    message_id: str | None = None
    """What the agent cites; a Discord request with none is a button press."""
    reply_to: str | None = None
    channel_id: str | None = None
    """Where a Discord request was posted."""
    is_self: bool = False
    """Teams: the agent's own post."""
    extra_attrs: Mapping[str, str] = field(default_factory=dict[str, str])
    """More attributes, in order, after the platform's own."""


@dataclass(frozen=True)
class ContextOptions:
    channel_id: str
    block: str | None = "thread_history"
    """`thread_history`, `thread_delta`, `channel_context`...; None: no history block."""
    block_attrs: Mapping[str, str] | None = None
    key_names: Sequence[str] = ()
    is_admin: bool = False
    author_name: str | None = None
    head: Sequence[str] = ()
    """Place elements; the bare `<channel>` element when empty."""
    tail: Sequence[str] = ()
    """Lines after the history block: reply targets, recent files, a history note."""
    unprompted: bool = False
    query_sources: Sequence[HistoryMessage] = ()
    """The messages a Discord request was merged from; more than one makes it composite."""
    wrapped: bool = True
    """False: no `<context>` tags (a Discord channel mention)."""
    lead: str | None = None
    """A line before `<context>` (Teams' controls)."""
    query_prefix: str = ""


def _attr_text(attrs: Mapping[str, str]) -> str:
    return "".join(f" {name}={quoteattr(value)}" for name, value in attrs.items())


def _oversized_note(omit_url: bool) -> str:
    if omit_url:
        return (
            f"Larger than {MAX_VISION_IMAGE_DIMENSION}px on an edge. Its URL is withheld: "
            "reading it at full size already ended a conversation in this thread."
        )
    return (
        "Larger than "
        f"{MAX_VISION_IMAGE_DIMENSION}px on an edge. Do NOT read this at full "
        "size — that inlines it at its original dimensions and the request is "
        "rejected terminally, which kills this conversation. Downscale a copy "
        "first, then read the copy."
    )


_TEAMS_DOWNLOAD_HINT = "short-lived download URL: fetch it now (curl to disk, then read it)"
_TEAMS_REPLACE_HINT = (
    "; to save an edited file over it, pass link and version to replace_channel_file"
)


def _render_attachment(platform: str, a: HistoryAttachment) -> str:
    name = quoteattr(a.name)
    if platform == DISCORD:
        attrs = f" filename={name}"
        if a.url is not None:
            attrs += f" url={quoteattr(a.url)}"
        attrs += f" content_type={quoteattr(a.media_type)} size={quoteattr(str(a.size))}"
        if a.width is not None and a.height is not None:
            attrs += f" width={quoteattr(str(a.width))} height={quoteattr(str(a.height))}"
        if a.oversized:
            attrs += f' oversized="true" note={quoteattr(_oversized_note(a.url is None))}'
        return f"<attachment{attrs}/>"
    if platform == TEAMS:
        if a.url is None:
            return f'<attachment name={name} fetchable="false"/>'
        if not (a.link and a.version):
            hint = quoteattr(_TEAMS_DOWNLOAD_HINT)
            return f"<attachment name={name} url={quoteattr(a.url)} hint={hint}/>"
        hint = quoteattr(_TEAMS_DOWNLOAD_HINT + _TEAMS_REPLACE_HINT)
        return (
            f"<attachment name={name} url={quoteattr(a.url)} link={quoteattr(a.link)}"
            f" version={quoteattr(a.version)} hint={hint}/>"
        )
    return (
        f"<attachment name={name} url={quoteattr(a.url or '')} mimetype={quoteattr(a.media_type)}/>"
    )


def _message_attrs(platform: str, m: HistoryMessage) -> str:
    bot = quoteattr("true" if m.is_bot else "false")
    if platform == DISCORD:
        attrs = (
            f" id={quoteattr(m.message_id or '')} author_name={quoteattr(m.username)}"
            f" user_id={quoteattr(m.user_id)} is_bot={bot} timestamp={quoteattr(m.timestamp)}"
        )
        return attrs + (f" reply_to={quoteattr(m.reply_to)}" if m.reply_to is not None else "")
    if platform == TEAMS:
        return (
            f" author_name={quoteattr(m.username)} user_id={quoteattr(m.user_id)} is_bot={bot}"
            + (' is_self="true"' if m.is_self else "")
            + f" timestamp={quoteattr(m.timestamp)} message_id={quoteattr(m.message_id or '')}"
        )
    return (
        f" user_id={quoteattr(m.user_id)} username={quoteattr(m.username)} is_bot={bot}"
        f" timestamp={quoteattr(m.timestamp)}"
    )


def render_message(message: HistoryMessage, platform: str = "slack") -> list[str]:
    """One message as XML lines; attribute values quoted, text escaped."""
    attrs = _message_attrs(platform, message) + _attr_text(message.extra_attrs)
    text = escape(message.text)
    if not message.attachments:
        return [f"<message{attrs}>{text}</message>"]
    lines = [f"<message{attrs}>", text, "<attachments>"]
    lines += [_render_attachment(platform, a) for a in message.attachments]
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


def _discord_identity(m: HistoryMessage) -> str:
    """Button input ids identify interactions, not citable messages."""
    if m.message_id is None:
        return ' kind="button"'
    return f" message_id={quoteattr(m.message_id)} channel_id={quoteattr(m.channel_id or '')}"


def _discord_query(trigger: HistoryMessage, options: ContextOptions) -> str:
    """`message_id` and `channel_id` name the person's own message, apart from the thread
    the answer goes in. A merged request has no single `reply_to`: `query_sources` ties each
    message to its own."""
    composite = len(options.query_sources) > 1
    attrs = (
        _discord_identity(trigger) + f" author_name={quoteattr(trigger.username)}"
        f" user_id={quoteattr(trigger.user_id)} timestamp={quoteattr(trigger.timestamp)}"
        f" is_admin={quoteattr('true' if options.is_admin else 'false')}"
        + (f" reply_to={quoteattr(trigger.reply_to)}" if trigger.reply_to and not composite else "")
        + (' unprompted="true"' if options.unprompted else "")
    )
    content = escape(trigger.text)
    if composite:
        sources = "".join(
            f"<message{_discord_identity(m)}"
            + (f" reply_to={quoteattr(m.reply_to)}" if m.reply_to is not None else "")
            + f">{escape(m.text)}</message>"
            for m in options.query_sources
        )
        content += f"<query_sources>{sources}</query_sources>"
    return f"<user_query{attrs}>{content}</user_query>"


def _teams_query(trigger: HistoryMessage, options: ContextOptions) -> str:
    author = f" author_name={quoteattr(trigger.username)}" if trigger.username else ""
    sent = f" timestamp={quoteattr(trigger.timestamp)}" if trigger.timestamp else ""
    return (
        f"<user_query{author} author_id={quoteattr(trigger.user_id)}{sent} "
        f'is_admin="{str(options.is_admin).lower()}"'
        + (' unprompted="true"' if options.unprompted else "")
        + f">{escape(trigger.text)}</user_query>"
    )


def render_query(platform: str, trigger: HistoryMessage, options: ContextOptions) -> str:
    """The `<user_query>`: `trigger` is the message being answered, its `user_id` the author
    (Slack: empty is a bare query)."""
    if platform == DISCORD:
        return _discord_query(trigger, options)
    if platform == TEAMS:
        return options.query_prefix + _teams_query(trigger, options)
    tag = user_query_open_tag(trigger.user_id, options.is_admin, options.author_name)
    return f"{tag}{escape(trigger.text)}</user_query>"


def render_frame(
    platform: str, body: Sequence[str], trigger: HistoryMessage, options: ContextOptions
) -> str:
    """The context around history already rendered to lines, and the request after it."""
    lines = [
        *(["<context>"] if options.wrapped else []),
        *(
            options.head
            or [f"<channel platform={quoteattr(platform)} id={quoteattr(options.channel_id)}/>"]
        ),
        render_keys_element(options.key_names),
    ]
    lines = [line for line in lines if line]
    if options.block is not None:
        attrs = options.block_attrs if options.block_attrs is not None else {"source": platform}
        lines.extend(untrusted_block(options.block, body, attrs))
    lines.extend(options.tail)
    if options.wrapped:
        lines.append("</context>")
    lines += ["", render_query(platform, trigger, options)]
    return "\n".join([options.lead, *lines] if options.lead is not None else lines)


def render_context(
    platform: str,
    history: Sequence[HistoryMessage],
    trigger: HistoryMessage,
    options: ContextOptions,
) -> str:
    """The context block and the request."""
    body = [line for message in history for line in render_message(message, platform)]
    return render_frame(platform, body, trigger, options)


# ---- Discord places ----

_DISCORD_THREAD_HINT = "to post a message in this thread, pass this id as channel_id"
_DISCORD_CHANNEL_HINT = (
    "the channel these turns belong to; agent-default and routing scopes key on this id, "
    "never on the thread id"
)

#: Why the agent may not see a message someone says they sent. Without it the
#: agent, asked about an earlier mention answered in a sibling thread, reads
#: the gap in its own context as a delivery failure and tells the user the
#: message "never reached me", which is false: every thread is its own session.
_DISCORD_DELIVERY_HINT = (
    "a turn starts when someone addresses you (an @mention or a reply to one of your posts) "
    "or, in a thread you follow, when you choose to answer; you see this thread's earlier "
    "messages as history, but every thread is a separate conversation with its own session, "
    "so you cannot see what was said or answered in other threads. If asked about an earlier "
    "message you have no record of, explain that it may be in another thread or was not "
    "addressed to you, and that other threads are separate conversations; never say it did "
    "not reach you or that you did not receive it. When the user_query has a reply_to id, "
    "the person is replying to that message: find it by that id in the history or in the "
    "reply_targets block, and answer about that message, not the latest one. "
    "The user_query message_id and channel_id identify the request message itself and "
    "where the human posted it, which may differ from your response thread. For merged "
    "requests, query_sources lists each original message with its own message_id, "
    "channel_id, text and optional reply_to; cite those ids for each source"
)


def discord_head(
    channel_id: str, channel_name: str | None, thread_id: str, thread_name: str
) -> list[str]:
    """The parent channel and current thread, told apart by `role` so the agent targets the
    thread, not the parent channel, when asked to post "here", then the delivery note.

    The thread hint is scoped to *posting*: `channel_id` means "where to put this message"
    for the send and credential-request tools, but "which scope to key on" for the scope
    tools, where only the parent channel id is ever right. So each role carries its own hint.
    """
    name = "" if channel_name is None else f" name={quoteattr(channel_name)}"
    return [
        f"<channel platform={quoteattr('discord')} id={quoteattr(channel_id)}"
        f" role={quoteattr('parent_channel')}{name} hint={quoteattr(_DISCORD_CHANNEL_HINT)}/>",
        f"<thread platform={quoteattr('discord')} id={quoteattr(thread_id)}"
        f" role={quoteattr('current_thread')} hint={quoteattr(_DISCORD_THREAD_HINT)}"
        f" name={quoteattr(thread_name)}/>",
        f"<delivery hint={quoteattr(_DISCORD_DELIVERY_HINT)}/>",
    ]


# ---- Teams places ----

_TEAMS_FILES_HINT = (
    "Files can't be attached in this channel until a Microsoft 365 admin grants daimon's app "
    "the Files.ReadWrite.All application permission, with admin consent. Mention it only if "
    "someone asks for a file here; do not swap in an artifact, report or notebook unless "
    "they ask for one."
)
_TEAMS_GROUP_FILES_HINT = (
    "Files can't be attached in a group chat. Mention it only if someone asks for a file "
    "here: they can ask you for it in their 1:1 chat with you."
)
# The same, for a person from another organisation, who has no 1:1 chat with the agent.
_TEAMS_GROUP_FILES_HINT_FOREIGN = (
    "Files can't be attached in a group chat. This person is from another organisation, so "
    "a 1:1 chat with Daimon can't be opened for them; don't suggest one. Mention it only if "
    "someone asks for a file here: paste short text inline."
)
# A 1:1 chat has no Graph thumbnails: the agent may picture a document itself
# (`output_delivery.is_preview_output`). Page one only: typst's PNG export of a multi-page
# document needs a page template. The default environment has no office suite.
_TEAMS_OWN_POSTS_HINT = (
    "Your latest messages in this chat, newest reply first. A reply spans several "
    "messages: its answer text (role answer), a status card (progress), a notice and a "
    "controls card (summary and buttons). To delete your reply or your last reply, pass "
    "this chat's id and any message_id of it to delete_reply. To edit or delete one "
    "message only, use edit_message or delete_message."
)
_TEAMS_DIRECT_FILES_HINT = (
    "When you deliver a .docx, .pptx, .xlsx or .pdf here, you may also save a PNG of its "
    "first page or slide to your outputs as <file name>.preview.png "
    "(report.pdf.preview.png for report.pdf): it shows inline as a picture and is never "
    "offered as a file. Only when you can render one cheaply, such as from a PDF with "
    "PyMuPDF (pip install pymupdf): pymupdf.open(pdf)[0].get_pixmap(dpi=96).save(png). "
    "No office renderer is installed by default."
)


def teams_head(
    kind: str,
    channel_id: str,
    *,
    thread_id: str = "",
    channel_name: str | None = None,
    channel_type: str | None = None,
    team_name: str | None = None,
    channel_files: bool | None = None,
    is_foreign: bool = False,
) -> list[str]:
    """The place elements of a Teams turn: a 1:1 chat, a channel thread or a group chat.

    A channel names its thread too, the id thread-scoped settings key on, and says with
    `files` whether a file saved now reaches the channel (`channel_files`; None in a chat).
    Where it does not, `files_hint` says an admin must grant the permission.
    """
    if kind == "channel":
        files = ""
        if channel_files is not None:
            files = f' files="{"available" if channel_files else "unavailable"}"'
        if channel_files is False:
            files += f" files_hint={quoteattr(_TEAMS_FILES_HINT)}"
        names = "".join(
            f" {attr}={quoteattr(value)}"
            for attr, value in (
                ("name", channel_name),
                ("type", channel_type),
                ("team_name", team_name),
            )
            if value
        )
        return [
            f'<channel platform="teams" id={quoteattr(channel_id)}'
            f' role="parent_channel"{names}{files}/>',
            f'<thread platform="teams" id={quoteattr(thread_id)} role="current_thread"/>',
        ]
    if kind == "group":
        name = f" name={quoteattr(channel_name)}" if channel_name else ""
        hint = quoteattr(_TEAMS_GROUP_FILES_HINT_FOREIGN if is_foreign else _TEAMS_GROUP_FILES_HINT)
        return [
            f'<channel platform="teams" id={quoteattr(channel_id)} role="group_chat"'
            f'{name} files="unavailable" files_hint={hint}/>'
        ]
    hint = quoteattr(_TEAMS_DIRECT_FILES_HINT)
    return [f'<channel platform="teams" id={quoteattr(channel_id)} files_hint={hint}/>']


def teams_own_posts(replies: Sequence[Sequence[tuple[str, str | None, str]]]) -> list[str]:
    """The agent's latest replies in a 1:1 chat, newest first, each its messages
    (message id, role, posted at)."""
    if not replies:
        return []
    return [
        f"<your_posts hint={quoteattr(_TEAMS_OWN_POSTS_HINT)}>",
        *(
            line
            for reply in replies
            for line in (
                "<reply>",
                *(
                    f"<post message_id={quoteattr(id_)}"
                    f"{f' role={quoteattr(role)}' if role else ''} timestamp={quoteattr(at)}/>"
                    for id_, role, at in reply
                ),
                "</reply>",
            )
        ),
        "</your_posts>",
    ]


def teams_history_unavailable(kind: str, reason: str) -> str:
    hint = (
        "daimon could not read this conversation's earlier messages from Microsoft Teams. "
        "If the request needs them, say so instead of guessing"
        + ("." if kind == "group" else "; read_channel or read_thread may still reach them.")
    )
    return f'<history status="unavailable" reason={quoteattr(reason)} hint={quoteattr(hint)}/>'
