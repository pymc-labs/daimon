"""Pure Block Kit state machine for Slack turn UX.

Ports the Discord ``embed.py`` state machine to Slack Block Kit, dropping the
color-based signaling.

Converts EmbedEvents and the turn's content into State and Block Kit dicts
with zero I/O dependencies. No ``slack_sdk`` or ``anthropic`` imports. The
in-progress card's words come from ``daimon.core.turn.status_lines``, shared
with Discord; ``escape_mrkdwn`` is imported from the sibling ``mrkdwn`` module
(same adapter package boundary; not a cross-adapter import).

Phase reference:
  THINKING     → *Thinking* · {elapsed}
  TOOL_RUNNING → *Working* · {elapsed}  (a tool call is running)
  DONE         → collapsed summary (terminal)
  ERROR        → ❌ collapsed summary (terminal)

Status surface shape (non-terminal):
  section  — *Thinking* · 12s  (headline)
  section  — ```tool lines```  (when the turn has made tool calls)
  section  — > {escaped draft}  (when text_preview is set; expand=True)
  actions  — Cancel button (action_id="cancel_turn"; style="danger"; no value)

Terminal collapse (DONE/ERROR):
  context  — {agent_name} · {elapsed}s · {in} in / {out} out [· {cost}]
             For ERROR: ❌ {reason} prepended

No color field anywhere — blocks only, no attachments.
Preview text entity-escaped via escape_mrkdwn (& first, then < >).
Cost/usage footer on terminal.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.turn.notices import TerminationNotice, fit_notice
from daimon.core.turn.state import TurnState
from daimon.core.turn.status_lines import (
    format_draft,
    format_headline,
    format_tool_lines,
    has_running_tool,
)

# Slack rejects a section block whose text exceeds 3,000 characters.
NOTICE_MAX_CHARS = 2900

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
    """An event fed into the Block Kit state machine.

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
class State:
    """Accumulated Block Kit state. Immutable — update() returns new instances."""

    phase: TurnPhase = TurnPhase.THINKING
    tool_lines: tuple[str, ...] = ()
    agent_name: str = ""
    started_at: float = 0.0
    usage_in: int = 0
    usage_out: int = 0
    cost_str: str | None = None
    text_preview: str | None = None
    error_reason: str = ""
    """Why the turn failed; leads the ERROR summary."""
    notice: str = ""
    """Rendered termination notice (mrkdwn); drawn above the ERROR summary."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EMOJI_CROSS = "❌"  # ❌

_TERMINAL_PHASES = frozenset({TurnPhase.DONE, TurnPhase.ERROR})

# Copy is byte-identical to the Discord adapter's orphan-retirement embed: the
# two adapters must say the same thing about the same event.
INTERRUPTED_NOTICE: str = (
    f"{_EMOJI_CROSS} This turn was interrupted by a restart and cannot be "
    "resumed. Nothing was lost on your side — mention me again to retry."
)

# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def update(state: State, event: EmbedEvent) -> State:
    """Return a new State with event applied.

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


def update_activity(state: State, turn: TurnState) -> State:
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


def to_blocks(
    state: State, *, now: float | None, cancel_key: str | None = None
) -> list[dict[str, Any]]:
    """Render State into a list of Slack Block Kit block dicts.

    Args:
        state: Current turn State.
        now: Current monotonic time from the caller (``time.monotonic()``).
             Pass ``None`` to omit the elapsed line.
        cancel_key: Optional per-turn routing key for the live Cancel button.

    Returns:
        A list of raw block dicts safe to pass directly to Slack's ``blocks=``
        parameter. No ``slack_sdk.models.blocks`` types — pure dicts (Pitfall 5).

    Non-terminal (THINKING / TOOL_RUNNING):
        - section  : ``*Thinking* · {elapsed}`` or ``*Working* · {elapsed}``
        - section  : the tool lines in a code block  (when there are any)
        - section  : > {escaped draft}  (when text_preview is set; expand=True)
        - actions  : Cancel button  (action_id="cancel_turn", style="danger")

    Terminal (DONE / ERROR):
        - context  : {agent_name} · {elapsed}s · {in} in / {out} out [· {cost}]
                     ERROR prepends ❌ {reason}, under a section with the
                     termination notice when one was rendered
        No actions block (cancel button removed on terminal).
    """
    if state.phase in _TERMINAL_PHASES:
        # Terminal collapse: one summary context block only.
        elapsed = int(now - state.started_at) if now is not None else 0
        tokens = f"{_fmt_tokens(state.usage_in)} in / {_fmt_tokens(state.usage_out)} out"
        parts: list[str] = [state.agent_name, f"{elapsed}s", tokens]
        if state.cost_str is not None:
            parts.append(state.cost_str)
        summary = " · ".join(parts)
        if state.phase is TurnPhase.ERROR:
            summary_text = f"{_EMOJI_CROSS} {state.error_reason or 'error'} · {summary}"
        else:
            summary_text = summary
        blocks: list[dict[str, Any]] = []
        if state.phase is TurnPhase.ERROR and state.notice:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": state.notice}})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": summary_text}]})
        return blocks

    # Non-terminal: the headline, the tool lines, then the latest draft.
    elapsed_seconds = now - state.started_at if now is not None and state.started_at else None
    headline = format_headline(
        is_working=state.phase is TurnPhase.TOOL_RUNNING,
        elapsed_seconds=elapsed_seconds,
        bold=lambda word: f"*{word}*",
    )
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": headline}},
    ]
    if state.tool_lines:
        tool_lines = escape_mrkdwn("\n".join(state.tool_lines))
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```\n{tool_lines}\n```"}}
        )
    # expand=True keeps Slack from folding the draft behind "see more".
    if state.text_preview:
        blocks.append(
            {
                "type": "section",
                "expand": True,
                "text": {"type": "mrkdwn", "text": f"> {escape_mrkdwn(state.text_preview)}"},
            }
        )

    # Cancel button — present only while the turn is running (non-terminal).
    # A per-turn value lets the listener route clicks before Slack returns the
    # message ts from chat.postMessage.
    cancel_button: dict[str, Any] = {
        "type": "button",
        "action_id": "cancel_turn",
        "text": {"type": "plain_text", "text": "Cancel"},
        "style": "danger",
    }
    if cancel_key is not None:
        cancel_button["value"] = cancel_key
    blocks.append(
        {
            "type": "actions",
            "elements": [cancel_button],
        }
    )

    return blocks


def to_interrupted_blocks() -> list[dict[str, Any]]:
    """Render the frozen status card a boot sweep leaves behind.

    Deliberately NOT ``to_blocks(State(phase=TurnPhase.ERROR, ...))``: a fresh
    boot process has the DB row and nothing else -- no agent name, no usage,
    no monotonic start -- so the terminal collapse would render an empty
    agent field and a misleading "0s · 0 in / 0 out" for a turn that may have
    run 40 minutes.

    Takes no arguments and emits no ``actions`` block, so the Cancel button is
    gone by construction -- there is no live turn left to cancel.
    """
    return [{"type": "section", "text": {"type": "mrkdwn", "text": INTERRUPTED_NOTICE}}]


def format_termination_notice(notice: TerminationNotice) -> str:
    """Draw the core notice as mrkdwn for the ERROR card.

    The headline is not repeated here: it is already the summary's reason.
    """
    lines = [escape_mrkdwn(notice.cause)]
    work = notice.work_line(lambda name: f"`{escape_mrkdwn(name.replace('`', ''))}`")
    if work is not None:
        lines.append(work)
    lines.append(notice.survived)
    lines.append(f"*Next:* {notice.next_step}")
    tail = f"`rid: {notice.request_id}`" if notice.request_id is not None else None
    return fit_notice(lines, tail=tail, limit=NOTICE_MAX_CHARS)
