"""A case's conversation as its platform hands it to a fresh session (`chat_history`)."""

from __future__ import annotations

from daimon.core.chat_history import ContextOptions, HistoryMessage, render_context
from daimon.core.evals.format import Case, ChatMessage, Participant


def _message(message: ChatMessage, people: dict[str, Participant]) -> HistoryMessage:
    person = people.get(message.author)
    return HistoryMessage(
        user_id=message.author,
        username=person.display_name if person else message.author,
        is_bot=bool(person and person.is_bot) or message.author == "bot",
        timestamp=message.at.isoformat(),
        text=message.text or "",
    )


def turn_text(case: Case) -> str:
    """The first message of an item's turn: the case's history, then its request."""
    surface = case.surface
    people = {p.id: p for p in [*surface.participants, surface.bot]}
    asking = case.turn.messages[-1]
    author = people.get(asking.author)
    place = surface.thread or surface.channel
    trigger = _message(asking, people)
    trigger = HistoryMessage(
        user_id=trigger.user_id, username=trigger.username, is_bot=False,
        timestamp=trigger.timestamp,
        text="\n\n".join(m.text for m in case.turn.messages if m.text),
    )  # fmt: skip
    return render_context(
        surface.platform,
        [_message(m, people) for m in case.history if not m.deleted],
        trigger,
        ContextOptions(
            channel_id=place.id if place else surface.conversation,
            is_admin=bool(author and author.role in ("owner", "admin")),
            author_name=author.display_name if author else None,
        ),
    )
