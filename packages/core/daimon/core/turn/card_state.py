"""Shared card phase, draft and tool activity transitions."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from daimon.core.platform_words import scrub_other_platforms
from daimon.core.turn.progress_plans import progress_plans_shown
from daimon.core.turn.state import TurnState
from daimon.core.turn.status_lines import (
    format_draft,
    format_plan_lines,
    format_tool_lines,
    has_running_tool,
    roll_draft,
)


class TurnPhase(Enum):
    THINKING = "thinking"
    TOOL_RUNNING = "tool_running"
    DONE = "done"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Event type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CardEvent:
    """An event fed into the embed state machine.

    kind discriminates the event:
    - "message": agent emitted a whole message; label is the full text
      (flattened and clipped to the draft length in ``update``) and joins
      the draft log
    - "draft": the same text revised in place (a provider's preview of the
      message in progress); replaces the latest draft line
    - "done": turn completed successfully
    - "error": turn failed; label is the error description

    Tool calls are not events here: ``update_activity`` reads them from the
    turn state on each render.
    """

    kind: Literal["message", "draft", "done", "error"]
    label: str = ""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CardState:
    """Accumulated embed state. Immutable — update() returns new instances."""

    phase: TurnPhase = TurnPhase.THINKING
    tool_lines: tuple[str, ...] = ()
    plan_lines: tuple[str, ...] = ()
    """The agent's latest progress plan as a checklist; empty unless progress plans are on."""
    agent_name: str = ""
    platform: str | None = None
    """The chat platform the card is on: the draft and tool lines never name another app."""
    header_customized: bool = False
    started_at: float = 0.0
    cost_str: str | None = None
    balance_str: str | None = None
    text_preview: str | None = None
    """The latest draft line."""
    earlier_drafts: tuple[str, ...] = ()
    """The draft lines before ``text_preview`` that the card still shows, oldest first."""
    error_reason: str = ""
    """Internal failure label from the event; never rendered directly."""
    notice_title: str = ""
    """Approved title for a terminal notice; never populated from exception text."""
    notice: str = ""
    """Rendered termination notice; the ERROR card's body."""


_TERMINAL_PHASES = frozenset({TurnPhase.DONE, TurnPhase.ERROR})


def update(state: CardState, event: CardEvent) -> CardState:
    """Return a new CardState with event applied.

    A message joins the draft log shown under the headline (the last few
    lines, oldest first); an empty one changes nothing. A draft revises the
    latest line. done and error move to the terminal phases.
    """
    if event.kind in ("message", "draft"):
        if not event.label:
            return state
        draft = format_draft(scrub_other_platforms(event.label, state.platform))
        if event.kind == "draft":
            return dataclasses.replace(state, text_preview=draft)
        earlier, latest = roll_draft(state.earlier_drafts, state.text_preview, draft)
        return dataclasses.replace(state, earlier_drafts=earlier, text_preview=latest)
    if event.kind == "done":
        return dataclasses.replace(state, phase=TurnPhase.DONE)
    return dataclasses.replace(state, phase=TurnPhase.ERROR, error_reason=event.label or "error")


def update_activity(state: CardState, turn: TurnState) -> CardState:
    """Fold the turn's tool calls into the card: Working while one runs, else Thinking.

    With progress plans on for the turn, the latest plan update becomes the
    checklist and leaves the tool lines. A no-op once the turn is terminal, so
    a late render cannot reopen the card.
    """
    if state.phase in _TERMINAL_PHASES:
        return state
    phase = TurnPhase.TOOL_RUNNING if has_running_tool(turn.content) else TurnPhase.THINKING
    plan_shown = progress_plans_shown()
    lines = format_tool_lines(
        turn.content, finished_ids=turn.finished_tool_ids, plan_shown=plan_shown
    )
    plan = format_plan_lines(turn.content) if plan_shown else ()
    return dataclasses.replace(
        state,
        phase=phase,
        tool_lines=tuple(scrub_other_platforms(line, state.platform) for line in lines),
        plan_lines=tuple(scrub_other_platforms(line, state.platform) for line in plan),
    )
