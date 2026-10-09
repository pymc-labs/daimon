"""Shared card phase, draft and tool activity transitions."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from daimon.core.turn.state import TurnState
from daimon.core.turn.status_lines import format_draft, format_tool_lines, has_running_tool


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
class CardState:
    """Accumulated embed state. Immutable — update() returns new instances."""

    phase: TurnPhase = TurnPhase.THINKING
    tool_lines: tuple[str, ...] = ()
    agent_name: str = ""
    header_customized: bool = False
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


_TERMINAL_PHASES = frozenset({TurnPhase.DONE, TurnPhase.ERROR})


def update(state: CardState, event: CardEvent) -> CardState:
    """Return a new CardState with event applied.

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


def update_activity(state: CardState, turn: TurnState) -> CardState:
    """Fold the turn's tool calls into the card: Working while one runs, else Thinking.

    A no-op once the turn is terminal, so a late render cannot reopen the card.
    """
    if state.phase in _TERMINAL_PHASES:
        return state
    phase = TurnPhase.TOOL_RUNNING if has_running_tool(turn.content) else TurnPhase.THINKING
    lines = format_tool_lines(turn.content, finished_ids=turn.finished_tool_ids)
    return dataclasses.replace(state, phase=phase, tool_lines=lines)
