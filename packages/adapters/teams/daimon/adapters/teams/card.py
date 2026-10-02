"""Pure status-card state and Teams message builders. No I/O.

The status card says what Discord's and Slack's say, in the words of
`daimon.core.turn.status_lines`: a Thinking or Working headline with the
elapsed time, the turn's tool lines, the latest draft, and a Cancel button.
The answer replaces the card as plain markdown messages: a card TextBlock
renders no code blocks, a message does.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from daimon.core.turn.notices import TerminationNotice, fit_notice
from daimon.core.turn.state import TurnState
from daimon.core.turn.status_lines import (
    format_draft,
    format_headline,
    format_tool_lines,
    has_running_tool,
)
from microsoft_teams.api import Account, MentionEntity, MessageActivityInput
from microsoft_teams.cards import (
    ActionSet,
    AdaptiveCard,
    CardElement,
    ExecuteAction,
    OpenUrlAction,
    TextBlock,
)

# A Teams message is capped by payload size (about 28 KB), not characters;
# 4 000 stays under it at 4 UTF-8 bytes each, the widest (an emoji).
TEAMS_LIMIT = 4_000
CANCEL_VERB = "cancel_turn"
INTERRUPTED_NOTICE = (
    "❌ This turn was interrupted by a restart and cannot be resumed. "
    "Nothing was lost on your side — message me again to retry."
)
CANCELLED_NOTICE = "Turn cancelled."
TOOLS_DONE_NOTICE = "✅ Done."
_FALLBACK_MAX_CHARS = 100


@dataclass(frozen=True, slots=True)
class CardState:
    """What the live status card shows. `on_*` return new instances."""

    started_at: float
    is_working: bool = False
    tool_lines: tuple[str, ...] = ()
    draft: str | None = None


def on_message(state: CardState, text: str) -> CardState:
    """The latest agent text becomes the draft; an empty one keeps the last."""
    return dataclasses.replace(state, draft=format_draft(text)) if text else state


def on_activity(state: CardState, turn: TurnState) -> CardState:
    """Fold the turn's tool calls into the card: Working while one runs, else Thinking."""
    lines = format_tool_lines(turn.content, finished_ids=turn.finished_tool_ids)
    return dataclasses.replace(state, is_working=has_running_tool(turn.content), tool_lines=lines)


def _card(body: list[CardElement], *, fallback: str) -> MessageActivityInput:
    return MessageActivityInput().add_card(AdaptiveCard(body=body, fallback_text=fallback))


ENABLE_FILES = (
    "I can't open this team's files yet. A Microsoft 365 admin can turn them on for this "
    "team with one sign-in; the first one in the organisation must be a global admin."
)


def enable_files_card(url: str) -> MessageActivityInput:
    """Offers the sign-in that grants daimon this team's SharePoint site."""
    body: list[CardElement] = [TextBlock(text=ENABLE_FILES, wrap=True)]
    action = OpenUrlAction(title="Enable files", url=url)
    card = AdaptiveCard(body=body, actions=[action], fallback_text=ENABLE_FILES)
    return MessageActivityInput().add_card(card)


def status_card(state: CardState, *, now: float, cancel_key: str) -> MessageActivityInput:
    """The live card. `cancel_key` routes a Cancel click to this turn.

    A TextBlock renders no code fence and drops single line breaks, so the
    tool lines are a monospace block with one paragraph each.
    """
    elapsed = now - state.started_at
    headline = format_headline(
        is_working=state.is_working, elapsed_seconds=elapsed, bold=lambda word: f"**{word}**"
    )
    body: list[CardElement] = [TextBlock(text=headline, wrap=True)]
    if state.tool_lines:
        lines = "\n\n".join(state.tool_lines)
        body.append(TextBlock(text=lines, font_type="Monospace", size="Small", wrap=True))
    if state.draft:
        body.append(TextBlock(text=state.draft, is_subtle=True, wrap=True))
    body.append(
        ActionSet(
            actions=[
                ExecuteAction(
                    title="Cancel",
                    verb=CANCEL_VERB,
                    data={"action": CANCEL_VERB, "turn": cancel_key},
                    style="destructive",
                )
            ]
        )
    )
    fallback = format_headline(
        is_working=state.is_working, elapsed_seconds=elapsed, bold=lambda word: word
    )
    return _card(body, fallback=fallback)


def termination_text(notice: TerminationNotice) -> str:
    """The notice as the ❌ card's text, under the Teams limit, request id kept.

    One paragraph per line: a card TextBlock drops single line breaks.
    """
    lines = [f"❌ {notice.headline}: {notice.cause}"]
    if (work := notice.work_line()) is not None:
        lines.append(work)
    lines += [notice.survived, f"Next: {notice.next_step}"]
    # `fit_notice` joins the tail with one newline; the leading one makes it a paragraph.
    tail = f"\nRequest id: {notice.request_id}" if notice.request_id is not None else None
    return fit_notice(["\n\n".join(lines)], tail=tail, limit=TEAMS_LIMIT)


def notice_card(text: str) -> MessageActivityInput:
    """A terminal card with no buttons: bailouts, failures, restarts.

    Clipped as Slack clips its notices; the fallback repeats it, so it gets less.
    """
    body = fit_notice([text], tail=None, limit=TEAMS_LIMIT)
    fallback = fit_notice([text], tail=None, limit=_FALLBACK_MAX_CHARS)
    return _card([TextBlock(text=body, wrap=True)], fallback=fallback)


ANSWERED_BELOW = "✅ Done. The answer is below."


def answer_message(
    text: str, *, is_last: bool, mention: Account | None = None
) -> MessageActivityInput:
    """One chunk of the answer; the last carries the feedback buttons.

    A completion ping leads with `mention` (an AAD object id is enough).
    """
    message = MessageActivityInput(text=text, text_format="markdown").add_ai_generated()
    if mention is not None:
        tag = f"<at>{(mention.name or 'you').replace('<', '').replace('>', '')}</at>"
        message.text = f"{tag}\n\n{text}"
        message.add_entity(MentionEntity(mentioned=mention, text=tag))
    return message.add_feedback() if is_last else message
