"""Pure embed state machine for Discord turn UX.

Converts EmbedEvents and the turn's content into EmbedState and EmbedData with
zero I/O dependencies. The in-progress card's words come from
`daimon.core.turn.status_lines`, shared with Slack; this module adds the
Discord markup and the phase colors. No discord or anthropic imports.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from daimon.adapters.discord.theme import (
    COLOR_GREEN,
    COLOR_IN_PROGRESS,
    COLOR_RED,
)
from daimon.core.turn.notices import TerminationNotice, fit_notice
from daimon.core.turn.state import TurnState
from daimon.core.turn.status_lines import (
    format_draft,
    format_headline,
    format_tool_lines,
    has_running_tool,
)

# ---------------------------------------------------------------------------
# Phase enum
# ---------------------------------------------------------------------------


class TurnPhase(Enum):
    THINKING = "thinking"
    TOOL_RUNNING = "tool_running"
    DONE = "done"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Event type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbedEvent:
    """An event fed into the embed state machine.

    kind discriminates the event:
    - "message": agent emitted intermediate text; label is the full text
      (flattened and clipped to the draft length in ``update``)
    - "done": turn completed successfully
    - "error": turn failed; label is the error description

    Tool calls are not events here: ``update_activity`` reads them from the
    turn state on each render.
    """

    kind: Literal["message", "done", "error"]
    label: str = ""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbedState:
    """Accumulated embed state. Immutable — update() returns new instances."""

    phase: TurnPhase = TurnPhase.THINKING
    tool_lines: tuple[str, ...] = ()
    agent_name: str = ""
    started_at: float = 0.0
    usage_in: int = 0
    usage_out: int = 0
    cost_str: str | None = None
    balance_str: str | None = None
    text_preview: str | None = None
    error_reason: str = ""
    """Why the turn failed; leads the ERROR card's footer."""
    notice: str = ""
    """Rendered termination notice; the ERROR card's body."""


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbedData:
    """Rendered embed data ready for Discord. No discord.py types."""

    phase: TurnPhase
    title: str
    description: str
    color: int
    footer: str | None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EMOJI_CROSS = "❌"  # ❌

_PHASE_COLOR: dict[TurnPhase, int] = {
    TurnPhase.THINKING: COLOR_IN_PROGRESS,
    TurnPhase.TOOL_RUNNING: COLOR_IN_PROGRESS,
    TurnPhase.DONE: COLOR_GREEN,
    TurnPhase.ERROR: COLOR_RED,
}

_TERMINAL_PHASES = frozenset({TurnPhase.DONE, TurnPhase.ERROR})

# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

# Discord caps an embed description at 4,096 characters.
_NOTICE_MAX_CHARS = 4000


def _escape_markdown(text: str) -> str:
    """Escape Discord markdown so truncated preview text renders literally
    (a clipped draft can otherwise leave unclosed code fences / bold markers)."""
    for char in r"\`*_~|>[]()#":
        text = text.replace(char, f"\\{char}")
    return text


def update(state: EmbedState, event: EmbedEvent) -> EmbedState:
    """Return a new EmbedState with event applied.

    A message replaces the draft shown under the tool lines; an empty one
    keeps the last draft. done and error move to the terminal phases.
    """
    if event.kind == "message":
        if not event.label:
            return state
        return dataclasses.replace(state, text_preview=format_draft(event.label))
    if event.kind == "done":
        return dataclasses.replace(state, phase=TurnPhase.DONE)
    return dataclasses.replace(state, phase=TurnPhase.ERROR, error_reason=event.label or "error")


def update_activity(state: EmbedState, turn: TurnState) -> EmbedState:
    """Fold the turn's tool calls into the card: Working while one runs, else Thinking.

    A no-op once the turn is terminal, so a late render cannot reopen the card.
    """
    if state.phase in _TERMINAL_PHASES:
        return state
    phase = TurnPhase.TOOL_RUNNING if has_running_tool(turn.content) else TurnPhase.THINKING
    lines = format_tool_lines(turn.content, finished_ids=turn.finished_tool_ids)
    return dataclasses.replace(state, phase=phase, tool_lines=lines)


def _fmt_tokens(n: int) -> str:
    """Humanize a token count: <1000 verbatim, else one-decimal k with trailing
    ``.0`` stripped (``320`` -> ``"320"``, ``1500`` -> ``"1.5k"``, ``12000`` -> ``"12k"``)."""
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"


def to_embed_data(state: EmbedState, *, now: float | None = None) -> EmbedData:
    """Render EmbedState into an EmbedData output shape.

    now: current monotonic time (pass time.monotonic() from caller).
    Footer is only set on terminal phases (DONE, ERROR).
    """
    color = _PHASE_COLOR[state.phase]

    if state.phase in _TERMINAL_PHASES:
        # Terminal turns collapse to ONE line: the headline, tool lines and
        # draft drop away, leaving just a summary in the footer. The green/red
        # bar alone signals outcome — DONE shows no checkmark; ERROR keeps the
        # ❌ + its reason so a failed turn still says why.
        elapsed = int(now - state.started_at) if now is not None else 0
        tokens = f"{_fmt_tokens(state.usage_in)} in / {_fmt_tokens(state.usage_out)} out"
        parts = [state.agent_name, f"{elapsed}s", tokens]
        if state.cost_str is not None:
            parts.append(state.cost_str)
        if state.balance_str is not None:
            parts.append(state.balance_str)
        summary = " · ".join(parts)
        description = ""
        if state.phase is TurnPhase.ERROR:
            footer = f"{_EMOJI_CROSS} {state.error_reason or 'error'} · {summary}"
            description = state.notice
        else:
            footer = summary
        return EmbedData(
            phase=state.phase, title="", description=description, color=color, footer=footer
        )

    # In progress: one embed, the headline, the tool lines, then the latest draft.
    elapsed_seconds = now - state.started_at if now is not None and state.started_at else None
    sections = [
        format_headline(
            is_working=state.phase is TurnPhase.TOOL_RUNNING,
            elapsed_seconds=elapsed_seconds,
            bold=lambda word: f"**{word}**",
        )
    ]
    if state.tool_lines:
        sections.append("```\n" + "\n".join(state.tool_lines) + "\n```")
    if state.text_preview:
        sections.append(f"> {_escape_markdown(state.text_preview)}")
    return EmbedData(
        phase=state.phase, title="", description="\n".join(sections), color=color, footer=None
    )


def format_termination_notice(notice: TerminationNotice) -> str:
    """Draw the core notice as the ERROR card's body, in Discord markdown.

    The headline is not repeated here: it is already the footer's reason.
    """
    lines = [_escape_markdown(notice.cause)]
    if (work := notice.work_line(lambda name: f"`{name.replace('`', '')}`")) is not None:
        lines.append(work)
    lines.append(notice.survived)
    lines.append(f"**Next:** {notice.next_step}")
    tail = f"`rid: {notice.request_id}`" if notice.request_id is not None else None
    return fit_notice(lines, tail=tail, limit=_NOTICE_MAX_CHARS)
