"""XML context builder for Discord thread history replay.

Builds XML context from a Discord thread's message history, suitable for
prepending to a user's message when responding in an existing thread.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape, quoteattr

from daimon.adapters.discord.vision import is_vision_image_attachment

import discord

# Number of parent-channel messages fetched when a channel mention creates a new thread.
# Bot replies are included so the agent sees the full conversational context.
CHANNEL_BACKFILL_LIMIT = 25


def _strip_bot_mention(content: str, bot_user_id: int | None) -> str:
    """Replace ``<@bot_id>`` and ``<@!bot_id>`` with ``@daimon``."""
    if bot_user_id is None:
        return content
    return re.sub(rf"<@!?{bot_user_id}>", "@daimon", content)


def _render_location(thread: discord.Thread) -> list[str]:
    """Render the parent-channel and current-thread elements for a thread turn.

    Both ids are exposed and differentiated by ``role`` so the agent can target
    the thread (not the parent channel) when asked to post "here".
    """
    thread_hint = "to post in this thread, pass this id as channel_id"
    return [
        f"<channel platform={quoteattr('discord')} id={quoteattr(str(thread.parent_id))}"
        f" role={quoteattr('parent_channel')}/>",
        f"<thread platform={quoteattr('discord')} id={quoteattr(str(thread.id))}"
        f" role={quoteattr('current_thread')} hint={quoteattr(thread_hint)}/>",
    ]


def _render_message(msg: discord.Message, bot_user_id: int | None) -> list[str]:
    """Render a single message as XML lines."""
    attrs = (
        f" author_name={quoteattr(msg.author.display_name)}"
        f" user_id={quoteattr(str(msg.author.id))}"
        f" is_bot={quoteattr(str(msg.author.bot).lower())}"
        f" timestamp={quoteattr(msg.created_at.isoformat())}"
    )
    content = escape(_strip_bot_mention(msg.content, bot_user_id))

    if not msg.attachments:
        return [f"<message{attrs}>{content}</message>"]

    lines = [f"<message{attrs}>", content, "<attachments>"]
    for att in msg.attachments:
        att_attrs = (
            f" filename={quoteattr(att.filename)}"
            f" url={quoteattr(att.url)}"
            f" content_type={quoteattr(att.content_type or 'unknown')}"
            f" size={quoteattr(str(att.size))}"
        )
        lines.append(f"<attachment{att_attrs}/>")
    lines.append("</attachments>")
    lines.append("</message>")
    return lines


async def build_context_xml(
    thread: discord.Thread,
    trigger: discord.Message,
    *,
    limit: int = 100,
    bot_user_id: int | None = None,
) -> tuple[str, list[discord.Attachment]]:
    """Build XML context from thread history for the turn driver.

    Returns ``(xml, image_attachments)`` where ``xml`` has
    ``<context>/<thread_history>`` wrapping prior messages and a
    ``<user_query>`` element for the trigger message, and
    ``image_attachments`` is the list of image-type attachments found in
    the thread history (for the caller to pass as vision content blocks).
    The trigger message is excluded from thread_history.
    """
    messages = [m async for m in thread.history(limit=limit)]
    messages.sort(key=lambda m: m.created_at)
    messages = [m for m in messages if m.id != trigger.id]

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
        and starter.id != trigger.id
        and starter.id not in {m.id for m in messages}
    ):
        messages.insert(0, starter)

    image_atts: list[discord.Attachment] = [
        att for msg in messages for att in msg.attachments if is_vision_image_attachment(att)
    ]

    lines: list[str] = [
        "<context>",
        *_render_location(thread),
        "<thread_history>",
    ]
    for msg in messages:
        lines.extend(_render_message(msg, bot_user_id))
    lines.append("</thread_history>")
    lines.append("</context>")

    # user_query sits outside <context>, separated by a blank line
    trigger_attrs = (
        f" author_name={quoteattr(trigger.author.display_name)}"
        f" user_id={quoteattr(str(trigger.author.id))}"
        f" timestamp={quoteattr(trigger.created_at.isoformat())}"
    )
    trigger_content = escape(_strip_bot_mention(trigger.content, bot_user_id))
    lines.append("")
    lines.append(f"<user_query{trigger_attrs}>{trigger_content}</user_query>")

    return "\n".join(lines), image_atts


async def build_delta_xml(
    thread: discord.Thread,
    trigger: discord.Message,
    *,
    after_message_id: int | None,
    bot_user_id: int | None = None,
) -> tuple[str, list[discord.Attachment]]:
    """Build XML context for a continuation turn (delta since watermark).

    Returns ``(xml, image_attachments)`` where ``xml`` is a
    ``<context>/<thread_delta>`` block containing all messages posted after
    ``after_message_id`` (human AND bot replies), plus a ``<user_query>``
    element identical to the one ``build_context_xml`` emits, and
    ``image_attachments`` is the list of image-type attachments found in the
    delta (for the caller to pass as vision content blocks).  The trigger is
    excluded from the delta; the bot's own messages are NOT excluded.

    Q1→(b) (SCOPING §3/§9): bot replies ARE included in the cross-speaker
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
    """
    if after_message_id is None:
        return await build_context_xml(thread, trigger, bot_user_id=bot_user_id)

    after_obj = discord.Object(id=after_message_id)
    messages = [m async for m in thread.history(after=after_obj, oldest_first=True)]
    # Intentionally minimal filter: only post-watermark, non-trigger messages.
    # Bot replies ARE included for cross-speaker coherence (Q1→b, SCOPING §9).
    # No backfill, no thread-starter prepend, no coalescing — the reused MA session
    # already holds prior context server-side; re-injecting it duplicates tokens and
    # re-triggers image replay from earlier turns.
    messages = [m for m in messages if m.id != trigger.id]
    messages.sort(key=lambda m: m.created_at)

    image_atts: list[discord.Attachment] = [
        att for msg in messages for att in msg.attachments if is_vision_image_attachment(att)
    ]

    lines: list[str] = [
        "<context>",
        *_render_location(thread),
        "<thread_delta>",
    ]
    for msg in messages:
        lines.extend(_render_message(msg, bot_user_id))
    lines.append("</thread_delta>")
    lines.append("</context>")

    trigger_attrs = (
        f" author_name={quoteattr(trigger.author.display_name)}"
        f" user_id={quoteattr(str(trigger.author.id))}"
        f" timestamp={quoteattr(trigger.created_at.isoformat())}"
    )
    trigger_content = escape(_strip_bot_mention(trigger.content, bot_user_id))
    lines.append("")
    lines.append(f"<user_query{trigger_attrs}>{trigger_content}</user_query>")

    return "\n".join(lines), image_atts


async def build_channel_context_xml(
    channel: discord.TextChannel,
    trigger: discord.Message,
    *,
    limit: int = CHANNEL_BACKFILL_LIMIT,
    bot_user_id: int | None = None,
) -> tuple[str, list[discord.Attachment]]:
    """Build XML context from parent channel history for a channel-mention turn.

    Called when the bot is @-mentioned directly in a channel (not in a thread).
    Returns ``(xml, image_attachments)`` where ``xml`` is a
    ``<channel_context count="N">`` block containing the last ``limit`` messages
    (trigger excluded, bot replies INCLUDED) followed by a ``<user_query>`` element
    identical to the one ``build_context_xml`` emits.  ``image_attachments`` is the
    list of vision-image attachments found in the backfill.

    Unlike ``build_delta_xml``, bot replies are not filtered — the channel context
    shows the agent's own prior responses so the agent understands the conversation.
    """
    messages = [m async for m in channel.history(limit=limit)]
    messages.sort(key=lambda m: m.created_at)
    messages = [m for m in messages if m.id != trigger.id]

    image_atts: list[discord.Attachment] = [
        att for msg in messages for att in msg.attachments if is_vision_image_attachment(att)
    ]

    lines: list[str] = [f'<channel_context count="{len(messages)}">']
    for msg in messages:
        lines.extend(_render_message(msg, bot_user_id))
    lines.append("</channel_context>")

    trigger_attrs = (
        f" author_name={quoteattr(trigger.author.display_name)}"
        f" user_id={quoteattr(str(trigger.author.id))}"
        f" timestamp={quoteattr(trigger.created_at.isoformat())}"
    )
    trigger_content = escape(_strip_bot_mention(trigger.content, bot_user_id))
    lines.append("")
    lines.append(f"<user_query{trigger_attrs}>{trigger_content}</user_query>")

    return "\n".join(lines), image_atts
