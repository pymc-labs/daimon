"""Pure status-card state and Teams message builders. No I/O.

The status card mirrors Slack's Block Kit card: phase title, elapsed time and
the last five tools, a preview of the latest agent text, and a Cancel button.
The answer replaces the card as plain markdown messages: a card TextBlock
renders no code blocks, a message does.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Literal

from daimon.adapters.teams.split import TEAMS_LIMIT
from daimon.core.turn.notices import TerminationNotice, fit_notice
from microsoft_teams.api import MessageActivityInput
from microsoft_teams.cards import ActionSet, AdaptiveCard, CardElement, ExecuteAction, TextBlock

CANCEL_VERB = "cancel_turn"
INTERRUPTED_NOTICE = (
    "❌ This turn was interrupted by a restart and cannot be resumed. "
    "Nothing was lost on your side — message me again to retry."
)
CANCELLED_NOTICE = "Turn cancelled."
_TRAIL_MAX = 5
_PREVIEW_MAX_CHARS = 250
_TITLES = {"thinking": "🧠 thinking", "tool_running": "⚙️ running tool"}

Phase = Literal["thinking", "tool_running"]


@dataclass(frozen=True, slots=True)
class CardState:
    """What the live status card shows. `update_*` return new instances."""

    agent_name: str
    started_at: float
    phase: Phase = "thinking"
    trail: tuple[str, ...] = ()
    preview: str | None = None


def on_thinking(state: CardState) -> CardState:
    return dataclasses.replace(state, phase="thinking")


def on_message(state: CardState, text: str) -> CardState:
    if not text:
        return on_thinking(state)
    preview = text if len(text) <= _PREVIEW_MAX_CHARS else text[:_PREVIEW_MAX_CHARS] + "…"
    return dataclasses.replace(state, phase="thinking", preview=preview)


def on_tool(state: CardState, name: str) -> CardState:
    trail = (*state.trail, name)[-_TRAIL_MAX:]
    return dataclasses.replace(state, phase="tool_running", trail=trail)


def format_elapsed(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes}m {rest}s"


def format_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"


def footer_text(
    state: CardState, *, now: float, tokens_in: int, tokens_out: int, cost: str | None
) -> str:
    """`agent · 12s · 1.2k in / 300 out · $0.01`, the terminal summary line."""
    parts = [
        state.agent_name,
        format_elapsed(int(now - state.started_at)),
        f"{format_tokens(tokens_in)} in / {format_tokens(tokens_out)} out",
    ]
    if cost is not None:
        parts.append(cost)
    return " · ".join(parts)


def _card(body: list[CardElement], *, fallback: str) -> MessageActivityInput:
    return MessageActivityInput().add_card(AdaptiveCard(body=body, fallback_text=fallback))


def status_card(state: CardState, *, now: float, cancel_key: str) -> MessageActivityInput:
    """The live card. `cancel_key` routes a Cancel click to this turn."""
    title = _TITLES[state.phase]
    lines = [f"⏱️ {format_elapsed(int(now - state.started_at))}"]
    lines += [f"⚙️ {tool}" for tool in state.trail]
    body: list[CardElement] = [
        TextBlock(text=title, weight="Bolder", wrap=True),
        TextBlock(text="\n\n".join(lines), is_subtle=True, size="Small", wrap=True),
    ]
    if state.preview:
        body.append(TextBlock(text=f"💬 {state.preview}", wrap=True))
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
    return _card(body, fallback=f"{title} …")


def termination_text(notice: TerminationNotice, *, footer: str) -> str:
    """The notice as the ❌ card's text, under the Teams limit, request id kept.

    One paragraph per line: a card TextBlock drops single line breaks.
    """
    lines = [f"❌ {notice.headline}: {notice.cause}"]
    if (work := notice.work_line()) is not None:
        lines.append(work)
    lines += [notice.survived, f"Next: {notice.next_step}"]
    rid = [f"Request id: {notice.request_id}"] if notice.request_id is not None else []
    # `fit_notice` joins the tail with one newline; the leading one makes it a paragraph.
    tail = "\n" + "\n\n".join([*rid, footer])
    return fit_notice(["\n\n".join(lines)], tail=tail, limit=TEAMS_LIMIT)


def notice_card(text: str) -> MessageActivityInput:
    """A terminal card with no buttons: bailouts, failures, restarts."""
    return _card([TextBlock(text=text, wrap=True)], fallback=text)


def answer_message(text: str, *, footer: str | None) -> MessageActivityInput:
    """One chunk of the answer. The last chunk carries the footer and feedback."""
    message = MessageActivityInput(text=text, text_format="markdown").add_ai_generated()
    if footer is not None:
        message.text = f"{text}\n\n*{footer}*"
        message.add_feedback()
    return message
