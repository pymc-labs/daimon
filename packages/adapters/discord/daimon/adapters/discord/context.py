"""XML context builder for Discord thread history replay.

Builds XML context from a Discord thread's message history, suitable for
prepending to a user's message when responding in an existing thread.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape, quoteattr

from daimon.adapters.discord.vision import (
    MAX_VISION_IMAGE_DIMENSION,
    is_oversized_image,
    is_vision_image_attachment,
)

import discord

# Number of parent-channel messages fetched when a channel mention creates a new thread.
# Bot replies are included so the agent sees the full conversational context.
CHANNEL_BACKFILL_LIMIT = 25


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


def _render_message(
    msg: discord.Message,
    bot_user_id: int | None,
    *,
    bot_display_name: str = "daimon",
    omit_oversized_image_urls: bool = False,
) -> list[str]:
    """Render a single message as XML lines.

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
    attrs = (
        f" author_name={quoteattr(msg.author.display_name)}"
        f" user_id={quoteattr(str(msg.author.id))}"
        f" is_bot={quoteattr(str(msg.author.bot).lower())}"
        f" timestamp={quoteattr(msg.created_at.isoformat())}"
    )
    content = escape(
        _strip_bot_mention(msg.content, bot_user_id, bot_display_name=bot_display_name)
    )

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
                "reading it at full size already terminated a session in this thread."
            )
        lines.append(f"<attachment{att_attrs}/>")
    lines.append("</attachments>")
    lines.append("</message>")
    return lines


def _render_user_query(
    trigger: discord.Message,
    bot_user_id: int | None,
    *,
    bot_display_name: str,
    is_admin: bool,
    unprompted: bool = False,
) -> str:
    """Render the ``<user_query>`` element for a turn's trigger message.

    All three builders use this renderer so initial, delta and reseeded
    context carry the same author, timestamp and permission attributes.
    ``is_admin`` uses the lowercase literal ``"true"``/``"false"`` so the
    model knows whether the caller can make an admin-gated change.
    """
    attrs = (
        f" author_name={quoteattr(trigger.author.display_name)}"
        f" user_id={quoteattr(str(trigger.author.id))}"
        f" timestamp={quoteattr(trigger.created_at.isoformat())}"
        f" is_admin={quoteattr('true' if is_admin else 'false')}"
        + (' unprompted="true"' if unprompted else "")
    )
    content = escape(
        _strip_bot_mention(trigger.content, bot_user_id, bot_display_name=bot_display_name)
    )
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
        lines.extend(
            _render_message(
                msg,
                bot_user_id,
                bot_display_name=bot_display_name,
                omit_oversized_image_urls=omit_oversized_image_urls,
            )
        )
    lines.append("</thread_history>")
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
        return await build_context_xml(
            thread,
            trigger,
            bot_user_id=bot_user_id,
            bot_display_name=bot_display_name,
            is_admin=is_admin,
            unprompted=unprompted,
        )

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
        lines.extend(
            _render_message(
                msg,
                bot_user_id,
                bot_display_name=bot_display_name,
                omit_oversized_image_urls=omit_oversized_image_urls,
            )
        )
    lines.append("</thread_delta>")
    lines.append("</context>")

    lines.append("")
    lines.append(
        _render_user_query(
            trigger,
            bot_user_id,
            bot_display_name=bot_display_name,
            is_admin=is_admin,
            unprompted=unprompted,
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
    """
    messages = [m async for m in channel.history(limit=limit)]
    messages.sort(key=lambda m: m.created_at)
    messages = [m for m in messages if m.id != trigger.id]

    image_atts: list[discord.Attachment] = [
        att for msg in messages for att in msg.attachments if is_vision_image_attachment(att)
    ]

    lines: list[str] = [*_render_location(thread), f'<channel_context count="{len(messages)}">']
    for msg in messages:
        lines.extend(
            _render_message(
                msg,
                bot_user_id,
                bot_display_name=bot_display_name,
                omit_oversized_image_urls=omit_oversized_image_urls,
            )
        )
    lines.append("</channel_context>")

    lines.append("")
    lines.append(
        _render_user_query(
            trigger, bot_user_id, bot_display_name=bot_display_name, is_admin=is_admin
        )
    )

    return "\n".join(lines), image_atts
