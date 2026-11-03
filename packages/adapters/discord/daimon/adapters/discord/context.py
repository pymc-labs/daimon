"""XML context builder for Discord thread history replay.

Builds XML context from a Discord thread's message history, suitable for
prepending to a user's message when responding in an existing thread.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Collection, Sequence
from datetime import timedelta
from xml.sax.saxutils import escape, quoteattr

import structlog
from daimon.adapters.discord.button_message import ButtonMessage
from daimon.adapters.discord.vision import is_oversized_image, is_vision_image_attachment
from daimon.core.media.vision import MAX_VISION_IMAGE_DIMENSION
from daimon.core.turn_keys import render_keys_element
from daimon.core.untrusted import untrusted_block

import discord

log = structlog.get_logger()

# Number of parent-channel messages fetched when a channel mention creates a new thread.
# Bot replies are included so the agent sees the full conversational context.
CHANNEL_BACKFILL_LIMIT = 25
# A bounded second scan keeps recent uploads visible after channel chatter
# pushes them outside the conversational window. Discord history is newest first.
CHANNEL_FILE_SCAN_LIMIT = 200
CHANNEL_FILE_MAX_AGE = timedelta(hours=24)
CHANNEL_FILE_LIMIT = 10

# Replayed messages come from everyone in the conversation, not only the
# person asking, so they ride in the shared untrusted envelope. Only the
# `<user_query>` after it is the request.
_HISTORY_SOURCE = {"source": "discord"}

# A replied-to message that is not part of the replayed conversation rides in its
# own `<reply_targets>` block. Its text is bounded so one huge post cannot swamp
# the turn; the cut is marked, not silent.
REPLY_TARGET_MAX_CHARS = 2000
# In a composite queued query the agent matches each target to the replying
# message by a short excerpt of what the replier said.
REPLY_EXCERPT_MAX_CHARS = 200
# Fetching an uncached target is best effort and must never hold up a turn.
REPLY_FETCH_TIMEOUT_S = 5.0
_TRUNCATION_MARK = "… [truncated]"


def _strip_bot_mention(
    content: str, bot_user_id: int | None, *, bot_display_name: str = "daimon"
) -> str:
    """Replace ``<@bot_id>`` and ``<@!bot_id>`` with ``@{bot_display_name}``."""
    if bot_user_id is None:
        return content
    # Callable repl, not a replacement string: a plain f-string here would let
    # a backslash or \g<...> in the configured name raise re.error (or expand
    # group refs) on every mention in every guild.
    return re.sub(rf"<@!?{bot_user_id}>", lambda _: f"@{bot_display_name}", content)


def _render_location(thread: discord.Thread) -> list[str]:
    """Render the parent-channel and current-thread elements for a turn.

    Both ids are exposed and differentiated by ``role`` so the agent can target
    the thread (not the parent channel) when asked to post "here". Names ride
    alongside the ids so the agent can say where it is, not just post there;
    ``thread.parent`` is None when the parent is not in the client cache, so the
    channel name is emitted only when it resolves.

    The thread hint is deliberately scoped to *posting*. ``channel_id`` is
    overloaded across the toolset: for the send and credential-request tools it
    means "where to put this message", and the thread id is right; for the
    scope tools (``set_agent_default``, ``clear_agent_default``,
    ``explain_agent_resolution``) it means "which scope to key on", and only
    the parent channel id is ever right. A hint phrased as a blanket rule for
    every ``channel_id`` parameter taught the second case wrongly, so each
    role carries its own hint instead.
    """
    thread_hint = "to post a message in this thread, pass this id as channel_id"
    channel_hint = (
        "the channel these turns belong to; agent-default and routing scopes key on this id, "
        "never on the thread id"
    )
    channel_name = "" if thread.parent is None else f" name={quoteattr(thread.parent.name)}"
    return [
        f"<channel platform={quoteattr('discord')} id={quoteattr(str(thread.parent_id))}"
        f" role={quoteattr('parent_channel')}{channel_name}"
        f" hint={quoteattr(channel_hint)}/>",
        f"<thread platform={quoteattr('discord')} id={quoteattr(str(thread.id))}"
        f" role={quoteattr('current_thread')} hint={quoteattr(thread_hint)}"
        f" name={quoteattr(thread.name)}/>",
    ]


#: Why the agent may not see a message someone says they sent. Without it the
#: agent, asked about an earlier mention answered in a sibling thread, reads
#: the gap in its own context as a delivery failure and tells the user the
#: message "never reached me", which is false: every thread is its own session.
_DELIVERY_HINT = (
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


def _render_delivery() -> str:
    """The trusted note on what reaches the agent, emitted on every thread turn."""
    return f"<delivery hint={quoteattr(_DELIVERY_HINT)}/>"


def _reply_target_id(msg: discord.Message) -> int | None:
    """The id of the message ``msg`` replies to, or None when it is not a reply.

    Forwards and other references are not replies, and a reference without a
    message id points at nothing the agent could look up.
    """
    reference = msg.reference
    if (
        not isinstance(reference, discord.MessageReference)
        or reference.type is not discord.MessageReferenceType.reply
    ):
        return None
    return reference.message_id if isinstance(reference.message_id, int) else None


def _embed_text(msg: discord.Message) -> str:
    """Title and description text of ``msg``'s embeds.

    A bot answer or status card is an embed with no message text; without this a
    reply to it would show the agent an empty message.
    """
    parts: list[str] = []
    for embed in msg.embeds:
        for piece in (embed.title, embed.description):
            if isinstance(piece, str) and piece.strip():
                parts.append(piece.strip())
    return "\n".join(parts)


async def _resolve_reply_target(
    home: discord.abc.Messageable, msg: discord.Message
) -> discord.Message | discord.DeletedReferencedMessage | None:
    """Find the message ``msg`` replies to: Discord's own copy, else one fetch.

    ``None`` means it could not be found (not cached and the fetch failed); the
    agent then keeps only the ``reply_to`` id.
    """
    reference = msg.reference
    if not isinstance(reference, discord.MessageReference):
        return None
    resolved = reference.resolved
    if isinstance(resolved, discord.Message | discord.DeletedReferencedMessage):
        return resolved
    target_id = reference.message_id
    if target_id is None:
        return None
    try:
        channel: object | None = home
        home_id = getattr(home, "id", None)
        if reference.channel_id != home_id:
            guild = getattr(home, "guild", None)
            channel = None if guild is None else guild.get_channel_or_thread(reference.channel_id)
        fetch = getattr(channel, "fetch_message", None)
        if fetch is None:
            return None
        async with asyncio.timeout(REPLY_FETCH_TIMEOUT_S):
            fetched = await fetch(target_id)
        return fetched if isinstance(fetched, discord.Message) else None
    except (discord.HTTPException, TimeoutError):
        return None


async def _reply_target_lines(
    home: discord.abc.Messageable,
    query_messages: Sequence[discord.Message],
    rendered_ids: Collection[int],
    *,
    bot_user_id: int | None,
    bot_display_name: str,
    omit_oversized_image_urls: bool,
) -> list[str]:
    """The ``<reply_targets>`` block for replies whose target the context does not show.

    A target already rendered in the history, delta or channel context, or part of
    the query itself, needs only the ``reply_to`` id and is skipped here, except in
    a merged query, where an id-only entry (``shown`` or ``unavailable``) still
    ties each reply to its target. Never
    raises: a reply target is a convenience, not a reason to fail a turn.
    """
    query_ids = {m.id for m in query_messages}
    composite = len(query_messages) > 1
    body: list[str] = []
    for replying in query_messages:
        try:
            target_id = _reply_target_id(replying)
            if target_id is None:
                continue
            origin = f" reply_from={quoteattr(str(replying.id))}"
            if composite:
                excerpt = _strip_bot_mention(
                    replying.content, bot_user_id, bot_display_name=bot_display_name
                )[:REPLY_EXCERPT_MAX_CHARS]
                origin += (
                    f" reply_from_author={quoteattr(replying.author.display_name)}"
                    f" reply_from_excerpt={quoteattr(excerpt)}"
                )
            # A merged query has no single `reply_to`, so every reply in it keeps
            # an entry naming its target, even when the target is shown elsewhere.
            if target_id in rendered_ids or target_id in query_ids:
                if composite:
                    body.append(f'<message id={quoteattr(str(target_id))} shown="true"{origin}/>')
                continue
            target = await _resolve_reply_target(home, replying)
            if target is None:
                if composite:
                    body.append(
                        f'<message id={quoteattr(str(target_id))} unavailable="true"{origin}/>'
                    )
                continue
            if isinstance(target, discord.DeletedReferencedMessage):
                body.append(f'<message id={quoteattr(str(target_id))} deleted="true"{origin}/>')
                continue
            body.extend(
                _render_message(
                    target,
                    bot_user_id,
                    bot_display_name=bot_display_name,
                    omit_oversized_image_urls=omit_oversized_image_urls,
                    max_content_chars=REPLY_TARGET_MAX_CHARS,
                    extra_attrs=origin,
                )
            )
        except Exception:
            log.warning("reply_target.render_failed", message_id=str(replying.id), exc_info=True)
    if not body:
        return []
    return untrusted_block("reply_targets", body, _HISTORY_SOURCE)


def _render_message(
    msg: discord.Message,
    bot_user_id: int | None,
    *,
    bot_display_name: str = "daimon",
    omit_oversized_image_urls: bool = False,
    max_content_chars: int | None = None,
    extra_attrs: str = "",
) -> list[str]:
    """Render a single message as XML lines.

    Every message carries its ``id``, and ``reply_to`` when it is a Discord reply,
    so the agent can tell which message a reply points at.
    ``max_content_chars`` bounds the text (a marked cut) and ``extra_attrs`` appends
    ready-made attributes; both serve the ``<reply_targets>`` block.

    Image attachments over the API's pixel cap are marked ``oversized="true"``
    and carry a warning, because the agent otherwise sees a plain URL, curls it,
    ``read``s it at full size, and the resulting rejection is terminal — it
    kills the session and (before recovery existed) the whole thread.

    ``omit_oversized_image_urls`` drops those URLs entirely rather than merely
    warning. Set only on a dead-session recovery reseed: there the previous
    session provably died, so a prompt-level warning the model may ignore is
    not good enough — withholding the URL is what actually breaks the
    recover-replay-die loop.
    """
    reply_to = _reply_target_id(msg)
    attrs = (
        f" id={quoteattr(str(msg.id))}"
        f" author_name={quoteattr(msg.author.display_name)}"
        f" user_id={quoteattr(str(msg.author.id))}"
        f" is_bot={quoteattr(str(msg.author.bot).lower())}"
        f" timestamp={quoteattr(msg.created_at.isoformat())}"
        + (f" reply_to={quoteattr(str(reply_to))}" if reply_to is not None else "")
    )
    text = _strip_bot_mention(msg.content, bot_user_id, bot_display_name=bot_display_name)
    if not text.strip():
        text = _embed_text(msg)
    if max_content_chars is not None and len(text) > max_content_chars:
        text = text[:max_content_chars] + _TRUNCATION_MARK
        attrs += ' truncated="true"'
    attrs += extra_attrs
    content = escape(text)

    if not msg.attachments:
        return [f"<message{attrs}>{content}</message>"]

    lines = [f"<message{attrs}>", content, "<attachments>"]
    for att in msg.attachments:
        oversized = is_oversized_image(att)
        att_attrs = f" filename={quoteattr(att.filename)}"
        if not (oversized and omit_oversized_image_urls):
            att_attrs += f" url={quoteattr(att.url)}"
        att_attrs += (
            f" content_type={quoteattr(att.content_type or 'unknown')}"
            f" size={quoteattr(str(att.size))}"
        )
        if att.width is not None and att.height is not None:
            att_attrs += f" width={quoteattr(str(att.width))} height={quoteattr(str(att.height))}"
        if oversized:
            att_attrs += ' oversized="true"'
            att_attrs += " note=" + quoteattr(
                "Larger than "
                f"{MAX_VISION_IMAGE_DIMENSION}px on an edge. Do NOT read this at full "
                "size — that inlines it at its original dimensions and the request is "
                "rejected terminally, which kills this conversation. Downscale a copy "
                "first, then read the copy."
                if not omit_oversized_image_urls
                else "Larger than "
                f"{MAX_VISION_IMAGE_DIMENSION}px on an edge. Its URL is withheld: "
                "reading it at full size already ended a conversation in this thread."
            )
        lines.append(f"<attachment{att_attrs}/>")
    lines.append("</attachments>")
    lines.append("</message>")
    return lines


def _request_identity_attrs(message: discord.Message) -> str:
    """Button input IDs identify interactions, not citable Discord messages."""
    if isinstance(message, ButtonMessage):
        return ' kind="button"'
    return (
        f" message_id={quoteattr(str(message.id))} channel_id={quoteattr(str(message.channel.id))}"
    )


def _render_user_query(
    trigger: discord.Message,
    bot_user_id: int | None,
    *,
    bot_display_name: str,
    is_admin: bool,
    unprompted: bool = False,
    query_text: str | None = None,
    query_messages: Sequence[discord.Message] = (),
) -> str:
    """Render the ``<user_query>`` element for a turn's trigger message.

    ``reply_to`` names the message the trigger replies to. A composite query
    carries no single ``reply_to``: ``query_sources`` maps each request to its
    target, and ``<reply_targets>`` supplies the targets' content.

    ``message_id`` and ``channel_id`` identify the human's actual message,
    independently of the response thread. Composite ``query_sources`` retain
    every source's identity and text, including repeated text and non-replies.

    All three builders use this renderer so initial, delta and reseeded
    context carry the same author, timestamp and permission attributes.
    ``is_admin`` uses the lowercase literal ``"true"``/``"false"`` so the
    model knows whether the caller can make an admin-gated change.
    """
    composite = len(query_messages) > 1
    reply_to = _reply_target_id(trigger) if not composite else None
    attrs = (
        _request_identity_attrs(trigger) + f" author_name={quoteattr(trigger.author.display_name)}"
        f" user_id={quoteattr(str(trigger.author.id))}"
        f" timestamp={quoteattr(trigger.created_at.isoformat())}"
        f" is_admin={quoteattr('true' if is_admin else 'false')}"
        + (f" reply_to={quoteattr(str(reply_to))}" if reply_to is not None else "")
        + (' unprompted="true"' if unprompted else "")
    )
    content = escape(
        _strip_bot_mention(
            trigger.content if query_text is None else query_text,
            bot_user_id,
            bot_display_name=bot_display_name,
        )
    )
    if composite:
        sources: list[str] = []
        for message in query_messages:
            target_id = _reply_target_id(message)
            source_attrs = _request_identity_attrs(message) + (
                f" reply_to={quoteattr(str(target_id))}" if target_id is not None else ""
            )
            text = escape(
                _strip_bot_mention(message.content, bot_user_id, bot_display_name=bot_display_name)
            )
            sources.append(f"<message{source_attrs}>{text}</message>")
        content += "<query_sources>" + "".join(sources) + "</query_sources>"
    return f"<user_query{attrs}>{content}</user_query>"


async def build_context_xml(
    thread: discord.Thread,
    trigger: discord.Message,
    *,
    limit: int = 100,
    bot_user_id: int | None = None,
    bot_display_name: str = "daimon",
    omit_oversized_image_urls: bool = False,
    is_admin: bool = False,
    unprompted: bool = False,
    key_names: Sequence[str] = (),
    query_text: str | None = None,
    query_message_ids: Sequence[int] = (),
    query_messages: Sequence[discord.Message] = (),
    excluded_message_ids: Sequence[str] = (),
) -> tuple[str, list[discord.Attachment]]:
    """Build XML context from thread history for the turn driver.

    Pass ``omit_oversized_image_urls=True`` on a dead-session recovery reseed
    so an image that already terminated a session cannot be fetched again.

    Returns ``(xml, image_attachments)`` where ``xml`` has
    ``<context>/<thread_history>`` wrapping prior messages and a
    ``<user_query>`` element for the trigger message, and
    ``image_attachments`` is the list of image-type attachments found in
    the thread history (for the caller to pass as vision content blocks).
    The trigger message is excluded from thread_history.

    ``unprompted=True`` marks the trigger as one nobody @mentioned (organic
    thread participation), so the agent knows it chose to speak.

    ``key_names`` names this agent's stored keys, names only (see
    `daimon.core.turn_keys`); empty renders no ``<keys>`` element at all.
    ``query_text`` replaces only the active query text; ``trigger`` still
    supplies identity and remains excluded from history.

    ``query_messages`` are the messages a composite query was composed from
    (trigger included); their replies are reported like the trigger's.
    ``excluded_message_ids`` lists messages to keep out of the history, such as
    the turn's own status card.
    """
    messages = [m async for m in thread.history(limit=limit)]
    messages.sort(key=lambda m: m.created_at)
    excluded_ids = {
        trigger.id,
        *query_message_ids,
        *(int(msg_id) for msg_id in excluded_message_ids),
    }
    messages = [m for m in messages if m.id not in excluded_ids]

    # Prepend the thread-starter message once if it exists, is not the trigger,
    # and is not already present in the fetched history.  thread.history() omits
    # the parent message that started the thread, so without this step the agent
    # would miss the original post that gave the thread its context.
    # Wrapped in try/except so a fetch failure (deleted message, permission denied)
    # never breaks the turn — we simply proceed without the starter.
    starter: discord.Message | None = thread.starter_message
    parent = thread.parent
    if starter is None and isinstance(parent, discord.TextChannel):
        try:
            starter = await parent.fetch_message(thread.id)
        except discord.HTTPException:
            starter = None
    if (
        starter is not None
        and starter.id not in excluded_ids
        and starter.id not in {m.id for m in messages}
    ):
        messages.insert(0, starter)

    image_atts: list[discord.Attachment] = [
        att for msg in messages for att in msg.attachments if is_vision_image_attachment(att)
    ]

    lines: list[str] = [
        "<context>",
        *_render_location(thread),
        _render_delivery(),
        render_keys_element(key_names),
    ]
    lines = [line for line in lines if line]
    body = [
        line
        for msg in messages
        for line in _render_message(
            msg,
            bot_user_id,
            bot_display_name=bot_display_name,
            omit_oversized_image_urls=omit_oversized_image_urls,
        )
    ]
    lines.extend(untrusted_block("thread_history", body, _HISTORY_SOURCE))
    queries = tuple(query_messages) or (trigger,)
    lines.extend(
        await _reply_target_lines(
            thread,
            queries,
            {m.id for m in messages},
            bot_user_id=bot_user_id,
            bot_display_name=bot_display_name,
            omit_oversized_image_urls=omit_oversized_image_urls,
        )
    )
    lines.append("</context>")

    # user_query sits outside <context>, separated by a blank line
    lines.append("")
    lines.append(
        _render_user_query(
            trigger,
            bot_user_id,
            bot_display_name=bot_display_name,
            is_admin=is_admin,
            unprompted=unprompted,
            query_text=query_text,
            query_messages=queries,
        )
    )

    return "\n".join(lines), image_atts


async def build_delta_xml(
    thread: discord.Thread,
    trigger: discord.Message,
    *,
    after_message_id: int | None,
    bot_user_id: int | None = None,
    bot_display_name: str = "daimon",
    omit_oversized_image_urls: bool = False,
    is_admin: bool = False,
    unprompted: bool = False,
    key_names: Sequence[str] = (),
    excluded_message_ids: Sequence[str] = (),
    query_text: str | None = None,
    query_message_ids: Sequence[int] = (),
    query_messages: Sequence[discord.Message] = (),
) -> tuple[str, list[discord.Attachment]]:
    """Build XML context for a continuation turn (delta since watermark).

    Returns ``(xml, image_attachments)`` where ``xml`` is a
    ``<context>/<thread_delta>`` block containing all messages posted after
    ``after_message_id`` (human AND bot replies), plus a ``<user_query>``
    element identical to the one ``build_context_xml`` emits, and
    ``image_attachments`` is the list of image-type attachments found in the
    delta (for the caller to pass as vision content blocks).  The trigger is
    excluded from the delta. Bot replies remain visible unless their IDs are
    explicitly listed as answer chunks already delivered to this session.

    Bot replies ARE included in the cross-speaker
    delta.  When a caller resumes after other participants spoke, their
    per-caller session must see what the agent told those participants so the
    agent does not appear to "forget" its own prior replies in the same visible
    thread.  The accepted cost is a small token overhead on every multi-party
    resume; single-speaker continuation is unaffected in correctness (server-
    side memory already holds the replies; the duplication is benign).

    History images remain URL-only — no new vision-block inlining.

    When ``after_message_id`` is ``None`` (no watermark — first turn or
    recreate re-seed), falls back to ``build_context_xml`` for a full
    snapshot.

    ``key_names`` names this agent's stored keys, names only (see
    `daimon.core.turn_keys`); empty renders no ``<keys>`` element at all.
    ``query_text`` replaces only the active query text, preserving the
    original trigger's identity and history exclusion.
    """
    if after_message_id is None:
        return await build_context_xml(
            thread,
            trigger,
            bot_user_id=bot_user_id,
            bot_display_name=bot_display_name,
            is_admin=is_admin,
            unprompted=unprompted,
            key_names=key_names,
            query_text=query_text,
            query_message_ids=query_message_ids,
            query_messages=query_messages,
            excluded_message_ids=excluded_message_ids,
        )

    after_obj = discord.Object(id=after_message_id)
    messages = [m async for m in thread.history(after=after_obj, oldest_first=True)]
    # Intentionally minimal filter: only post-watermark, non-trigger messages.
    # Bot replies ARE included for cross-speaker coherence.
    # No backfill, no thread-starter prepend, no coalescing — the reused MA session
    # already holds prior context server-side; re-injecting it duplicates tokens and
    # re-triggers image replay from earlier turns.
    excluded_ids = {
        trigger.id,
        *query_message_ids,
        *(int(msg_id) for msg_id in excluded_message_ids),
    }
    messages = [m for m in messages if m.id not in excluded_ids]
    messages.sort(key=lambda m: m.created_at)

    image_atts: list[discord.Attachment] = [
        att for msg in messages for att in msg.attachments if is_vision_image_attachment(att)
    ]

    lines: list[str] = [
        "<context>",
        *_render_location(thread),
        _render_delivery(),
        render_keys_element(key_names),
    ]
    lines = [line for line in lines if line]
    body = [
        line
        for msg in messages
        for line in _render_message(
            msg,
            bot_user_id,
            bot_display_name=bot_display_name,
            omit_oversized_image_urls=omit_oversized_image_urls,
        )
    ]
    lines.extend(untrusted_block("thread_delta", body, _HISTORY_SOURCE))
    queries = tuple(query_messages) or (trigger,)
    lines.extend(
        await _reply_target_lines(
            thread,
            queries,
            {m.id for m in messages},
            bot_user_id=bot_user_id,
            bot_display_name=bot_display_name,
            omit_oversized_image_urls=omit_oversized_image_urls,
        )
    )
    lines.append("</context>")

    lines.append("")
    lines.append(
        _render_user_query(
            trigger,
            bot_user_id,
            bot_display_name=bot_display_name,
            is_admin=is_admin,
            unprompted=unprompted,
            query_text=query_text,
            query_messages=queries,
        )
    )

    return "\n".join(lines), image_atts


async def build_channel_context_xml(
    channel: discord.TextChannel,
    trigger: discord.Message,
    *,
    thread: discord.Thread,
    limit: int = CHANNEL_BACKFILL_LIMIT,
    bot_user_id: int | None = None,
    bot_display_name: str = "daimon",
    omit_oversized_image_urls: bool = False,
    is_admin: bool = False,
    key_names: Sequence[str] = (),
    query_text: str | None = None,
    query_message_ids: Sequence[int] = (),
    query_messages: Sequence[discord.Message] = (),
) -> tuple[str, list[discord.Attachment]]:
    """Build XML context from parent channel history for a channel-mention turn.

    Called when the bot is @-mentioned directly in a channel (not in a thread).
    ``thread`` is the thread this turn answers in — the caller has already
    created it — and supplies the location elements the agent needs to post
    "here"; without them the agent has no channel id at all and asks the user
    to paste a channel link.
    Returns ``(xml, image_attachments)`` where ``xml`` is a location block
    followed by a
    ``<channel_context count="N">`` block containing the last ``limit`` messages
    (trigger excluded, bot replies INCLUDED) followed by a ``<user_query>`` element
    identical to the one ``build_context_xml`` emits.  ``image_attachments`` is the
    list of vision-image attachments found in the backfill.

    Unlike ``build_delta_xml``, bot replies are not filtered — the channel context
    shows the agent's own prior responses so the agent understands the conversation.

    ``key_names`` names this agent's stored keys, names only (see
    `daimon.core.turn_keys`); empty renders no ``<keys>`` element at all.
    """
    try:
        recent = [m async for m in channel.history(limit=limit)]
    except discord.HTTPException:
        recent = []

    # The first page is the conversational context. Scan only older messages
    # for files, using the same channel visibility as that first read. Neither
    # older message text nor history images are added to the conversation.
    older_files: list[str] = []
    cutoff = trigger.created_at - CHANNEL_FILE_MAX_AGE
    if (
        0 < limit < CHANNEL_FILE_SCAN_LIMIT
        and len(recent) == limit
        and recent[-1].created_at >= cutoff
    ):
        seen_ids = {m.id for m in recent}
        try:
            async for msg in channel.history(
                limit=CHANNEL_FILE_SCAN_LIMIT - len(recent),
                before=discord.Object(id=recent[-1].id),
            ):
                if msg.created_at < cutoff:
                    break
                if msg.id == trigger.id or msg.id in seen_ids:
                    continue
                for att in msg.attachments:
                    older_files.append(
                        f"<attachment filename={quoteattr(att.filename)}"
                        f" url={quoteattr(att.url)}"
                        f" uploader={quoteattr(msg.author.display_name)}"
                        f" uploaded_at={quoteattr(msg.created_at.isoformat())}"
                        f" jump_url={quoteattr(msg.jump_url)}/>"
                    )
                    if len(older_files) == CHANNEL_FILE_LIMIT:
                        break
                if len(older_files) == CHANNEL_FILE_LIMIT:
                    break
        except Exception:
            # The file list is optional: any failed or dropped read (an HTTP
            # error, a timeout, a reset connection) keeps the conversation and
            # leaves the section out. Cancellation still propagates.
            older_files = []

    messages = recent
    messages.sort(key=lambda m: m.created_at)
    queries = tuple(query_messages) or (trigger,)
    excluded_ids = {trigger.id, *query_message_ids, *(m.id for m in queries)}
    messages = [m for m in messages if m.id not in excluded_ids]

    image_atts: list[discord.Attachment] = [
        att for msg in messages for att in msg.attachments if is_vision_image_attachment(att)
    ]

    lines: list[str] = [
        *_render_location(thread),
        _render_delivery(),
        render_keys_element(key_names),
    ]
    lines = [line for line in lines if line]
    body = [
        line
        for msg in messages
        for line in _render_message(
            msg,
            bot_user_id,
            bot_display_name=bot_display_name,
            omit_oversized_image_urls=omit_oversized_image_urls,
        )
    ]
    lines.extend(
        untrusted_block("channel_context", body, {"count": str(len(messages)), **_HISTORY_SOURCE})
    )
    lines.extend(
        await _reply_target_lines(
            channel,
            queries,
            {m.id for m in messages},
            bot_user_id=bot_user_id,
            bot_display_name=bot_display_name,
            omit_oversized_image_urls=omit_oversized_image_urls,
        )
    )
    if older_files:
        lines.extend(untrusted_block("recent_channel_files", older_files, _HISTORY_SOURCE))

    lines.append("")
    lines.append(
        _render_user_query(
            trigger,
            bot_user_id,
            bot_display_name=bot_display_name,
            is_admin=is_admin,
            query_text=query_text,
            query_messages=queries,
        )
    )

    return "\n".join(lines), image_atts
