"""A case's conversation as its platform hands it to a fresh session: each platform's own
context shape from `chat_history`, so the agent reads what it would in production."""

from __future__ import annotations

from collections.abc import Mapping

from daimon.core.chat_history import (
    ContextOptions,
    HistoryMessage,
    discord_head,
    render_context,
    teams_head,
)
from daimon.core.evals.format import Case, ChatMessage, Participant, Surface

_THREAD_NAME_CHARS = 100
"""Discord names a thread it starts from a message with the message's first 100 characters."""


def _message(
    message: ChatMessage,
    people: Mapping[str, Participant],
    bot_id: str = "bot",
    *,
    extra: Mapping[str, str] | None = None,
) -> HistoryMessage:
    person = people.get(message.author)
    mine = message.author in ("bot", bot_id)
    return HistoryMessage(
        user_id=message.author,
        username=person.display_name if person else message.author,
        is_bot=bool(person and person.is_bot) or mine,
        timestamp=message.at.isoformat(),
        text=message.text or "",
        message_id=message.id,
        reply_to=message.reply_to,
        is_self=mine,
        extra_attrs=extra or {},
    )


def _discord(case: Case, history: list[HistoryMessage], trigger: HistoryMessage,
             options: ContextOptions) -> str:  # fmt: skip
    """Thread turns replay the thread; a channel mention replays the channel and answers in
    the thread Discord starts from the mention (same id, named by the message's start)."""
    surface = case.surface
    sent = case.turn.messages
    channel = surface.channel
    if surface.conversation in ("dm", "group_dm") or channel is None:
        return render_context("discord", history, trigger, options)
    in_thread = surface.thread is not None
    thread = surface.thread
    options = ContextOptions(
        channel_id=channel.id,
        block="thread_history" if in_thread else "channel_context",
        block_attrs={"source": "discord"}
        if in_thread
        else {"count": str(len(history)), "source": "discord"},
        is_admin=options.is_admin,
        head=discord_head(
            channel.id,
            channel.name,
            thread.id if thread else sent[-1].id,
            thread.name if thread else trigger.text[:_THREAD_NAME_CHARS],
        ),
        query_sources=[_message(m, {}) for m in sent] if len(sent) > 1 else (),
        unprompted=options.unprompted,
        wrapped=in_thread,
    )
    return render_context("discord", history, trigger, options)


def _teams(surface: Surface, post: str, history: list[HistoryMessage], trigger: HistoryMessage,
           options: ContextOptions) -> str:  # fmt: skip
    """`post` is the request's message id, the thread a channel mention starts.
    A 1:1 chat replays nothing; a group chat its newest messages; a channel thread its
    messages, and a channel mention the channel's, each naming its thread."""
    channel = surface.channel
    kind = {"dm": "dm", "group_dm": "group"}.get(surface.conversation, "channel")
    channel_id = channel.id if channel else surface.conversation
    head = teams_head(
        kind,
        channel_id,
        thread_id=surface.thread.id if surface.thread else f"{channel_id};messageid={post}",
        channel_name=channel.name if channel else None,
        team_name=surface.workspace.name if surface.workspace else None,
        is_foreign=any(p.is_requester and p.role == "external" for p in surface.participants),
    )
    block = {
        "dm": None,
        "group": "chat_history",
        "channel": "thread_history" if surface.thread else "channel_context",
    }[kind]
    attrs: dict[str, str] = {"source": "teams"}
    if block == "channel_context":
        attrs["count"] = str(len(history))
    return render_context(
        "teams", [] if block is None else history, trigger,
        ContextOptions(
            channel_id=channel_id, block=block,
            block_attrs=attrs, is_admin=options.is_admin, head=head,
            unprompted=options.unprompted,
        ),
    )  # fmt: skip


def turn_text(case: Case) -> str:
    """The first message of an item's turn: the case's history, then its request."""
    surface = case.surface
    people = {p.id: p for p in [*surface.participants, surface.bot]}
    asking = case.turn.messages[-1]
    author = people.get(asking.author)
    place = surface.thread or surface.channel
    platform = surface.platform
    channel_post = platform == "teams" and surface.conversation == "channel"
    history = [
        _message(
            m,
            people,
            surface.bot.id,
            extra={"thread_id": f"{place.id if place else ''};messageid={m.thread_id or m.id}"}
            if channel_post and not surface.thread
            else None,
        )
        for m in case.history
        if not m.deleted
    ]
    trigger = _message(asking, people, surface.bot.id)
    trigger = HistoryMessage(
        user_id=trigger.user_id, username=trigger.username, is_bot=False,
        timestamp=trigger.timestamp,
        text="\n\n".join(m.text for m in case.turn.messages if m.text),
        message_id=asking.id, channel_id=surface.channel.id if surface.channel else None,
        reply_to=asking.reply_to,
    )  # fmt: skip
    options = ContextOptions(
        channel_id=place.id if place else surface.conversation,
        is_admin=bool(author and author.role in ("owner", "admin")),
        author_name=author.display_name if author else None,
        unprompted=case.turn.trigger == "auto",
    )
    if platform == "discord":
        return _discord(case, history, trigger, options)
    if platform == "teams":
        return _teams(surface, asking.id, history, trigger, options)
    return render_context(platform, history, trigger, options)
