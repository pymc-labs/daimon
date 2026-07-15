"""Pure Block Kit state machine for Slack turn UX.

Ports the Discord ``embed.py`` state machine to Slack Block Kit, dropping the
color-based signaling (D-03: emoji carries the phase signal instead).

Converts EmbedEvents into State and Block Kit dicts with zero I/O dependencies.
Imports only stdlib — no ``slack_sdk``, ``anthropic``, or ``daimon.core`` imports.
``escape_mrkdwn`` is imported from the sibling ``mrkdwn`` module (same adapter
package boundary; not a cross-adapter import).

Phase reference:
  THINKING    → 🧠 thinking
  TOOL_RUNNING → ⚙️ running tool
  DONE        → ✅ complete   (terminal)
  ERROR       → ❌ error      (terminal)

Status surface shape (non-terminal):
  section  — *{phase title}*  (bold emoji + label)
  context  — ⏱️ {elapsed}  ⚙️ {tool} … (elapsed + trail entries)
  section  — 💬 {escaped preview}  (when text_preview is set; expand=True)
  actions  — Cancel button (action_id="cancel_turn"; style="danger"; no value)

Terminal collapse (DONE/ERROR):
  context  — {agent_name} · {elapsed}s · {in} in / {out} out [· {cost}]
             For ERROR: ❌ {reason} prepended

D-03: No color field anywhere — blocks only, no attachments.
D-07: Preview text entity-escaped via escape_mrkdwn (& first, then < >).
D-08: Cost/usage footer on terminal.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from daimon.adapters.slack.mrkdwn import escape_mrkdwn

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
    - "thinking": agent is thinking or generating text
    - "message": agent emitted intermediate text; label is the full text
      (capped to the preview length in ``update``)
    - "tool_use": a tool was invoked; label is the tool name (never args)
    - "done": turn completed successfully
    - "error": turn failed; label is the error description

    Per threat model T-13-01: label carries tool name only for tool_use events,
    never tool arguments.
    """

    kind: Literal["thinking", "message", "tool_use", "done", "error"]
    label: str = ""


# ---------------------------------------------------------------------------
# Trail entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrailEntry:
    """A single entry in the activity trail."""

    emoji: str
    text: str


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class State:
    """Accumulated Block Kit state. Immutable — update() returns new instances."""

    phase: TurnPhase = TurnPhase.THINKING
    trail: tuple[TrailEntry, ...] = ()
    agent_name: str = ""
    started_at: float = 0.0
    usage_in: int = 0
    usage_out: int = 0
    cost_str: str | None = None
    text_preview: str | None = None


# ---------------------------------------------------------------------------
# Emoji constants
# ---------------------------------------------------------------------------

_EMOJI_BRAIN = "\U0001f9e0"  # 🧠
_EMOJI_GEAR = "⚙️"  # ⚙️
_EMOJI_CHECK = "✅"  # ✅
_EMOJI_CROSS = "❌"  # ❌

_KIND_EMOJI: dict[str, str] = {
    "thinking": _EMOJI_BRAIN,
    "message": _EMOJI_BRAIN,
    "tool_use": _EMOJI_GEAR,
    "done": _EMOJI_CHECK,
    "error": _EMOJI_CROSS,
}

_KIND_PHASE: dict[str, TurnPhase] = {
    "thinking": TurnPhase.THINKING,
    "message": TurnPhase.THINKING,
    "tool_use": TurnPhase.TOOL_RUNNING,
    "done": TurnPhase.DONE,
    "error": TurnPhase.ERROR,
}

_PHASE_TITLE: dict[TurnPhase, str] = {
    TurnPhase.THINKING: f"{_EMOJI_BRAIN} thinking",
    TurnPhase.TOOL_RUNNING: f"{_EMOJI_GEAR} running tool",
    TurnPhase.DONE: f"{_EMOJI_CHECK} complete",
    TurnPhase.ERROR: f"{_EMOJI_CROSS} error",
}

_TERMINAL_PHASES = frozenset({TurnPhase.DONE, TurnPhase.ERROR})

# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

_TRAIL_MAX = 5

_TEXT_PREVIEW_MAX_CHARS = 250


def update(state: State, event: EmbedEvent) -> State:
    """Return a new State with event applied.

    Trail is capped at _TRAIL_MAX (5) entries — keeps the last 5.
    Phase transitions to match event kind.

    Messages do not enter the trail: the latest agent message text is held in
    ``text_preview`` and rendered as its own preview section block,
    so reasoning is readable instead of truncated to a one-line snippet.

    Thinking events update phase only — MA carries no thinking text to surface.
    A bare "thinking" trail line would be empty noise next to real tool lines.
    """
    new_phase = _KIND_PHASE[event.kind]
    if event.kind == "thinking":
        # agent.thinking is a contentless progress ping — phase only, no trail.
        return dataclasses.replace(state, phase=new_phase)

    if event.kind == "message":
        if not event.label:
            return dataclasses.replace(state, phase=new_phase)
        preview = (
            event.label[:_TEXT_PREVIEW_MAX_CHARS] + "…"
            if len(event.label) > _TEXT_PREVIEW_MAX_CHARS
            else event.label
        )
        return dataclasses.replace(state, phase=new_phase, text_preview=preview)

    emoji = _KIND_EMOJI[event.kind]
    text: str
    if event.kind == "tool_use":
        text = event.label
    elif event.kind == "done":
        text = "complete"
    else:  # "error"
        text = event.label if event.label else "error"

    new_entry = TrailEntry(emoji=emoji, text=text)
    current = state.trail
    if len(current) >= _TRAIL_MAX:
        updated_trail: tuple[TrailEntry, ...] = current[-(_TRAIL_MAX - 1) :] + (new_entry,)
    else:
        updated_trail = current + (new_entry,)

    return dataclasses.replace(state, phase=new_phase, trail=updated_trail)


def _fmt_tokens(n: int) -> str:
    """Humanize a token count: <1000 verbatim, else one-decimal k with trailing
    ``.0`` stripped (``320`` -> ``"320"``, ``1500`` -> ``"1.5k"``, ``12000`` -> ``"12k"``)."""
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"


def _fmt_elapsed(total_seconds: int) -> str:
    """Format elapsed seconds as ``42s`` or ``2m 3s``."""
    total_seconds = max(0, total_seconds)
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}m {seconds}s"


def to_blocks(state: State, *, now: float | None) -> list[dict[str, Any]]:
    """Render State into a list of Slack Block Kit block dicts.

    Args:
        state: Current turn State.
        now: Current monotonic time from the caller (``time.monotonic()``).
             Pass ``None`` to omit the elapsed line.

    Returns:
        A list of raw block dicts safe to pass directly to Slack's ``blocks=``
        parameter. No ``slack_sdk.models.blocks`` types — pure dicts (Pitfall 5).

    Non-terminal (THINKING / TOOL_RUNNING):
        - section  : ``*{emoji phase title}*``
        - context  : ⏱️ {elapsed}  ⚙️ {tool} … (when trail or now is set)
        - section  : 💬 {escaped preview}  (when text_preview is set; expand=True)
        - actions  : Cancel button  (action_id="cancel_turn", style="danger")

    Terminal (DONE / ERROR):
        - context  : {agent_name} · {elapsed}s · {in} in / {out} out [· {cost}]
                     ERROR prepends ❌ {reason}
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
            reason = state.trail[-1].text if state.trail else "error"
            summary_text = f"{_EMOJI_CROSS} {reason} · {summary}"
        else:
            summary_text = summary
        return [
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": summary_text}],
            }
        ]

    # Non-terminal: build the status surface blocks.
    title = _PHASE_TITLE[state.phase]
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}},
    ]

    # Context block: elapsed marker + trail entries (omitted when both are absent).
    context_lines: list[str] = []
    if now is not None and state.started_at:
        context_lines.append(f"⏱️ {_fmt_elapsed(int(now - state.started_at))}")
    context_lines += [f"{e.emoji} {e.text}" for e in state.trail]
    if context_lines:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "\n".join(context_lines)}],
            }
        )

    # Preview section: latest agent message text, entity-escaped, expand=True.
    if state.text_preview:
        blocks.append(
            {
                "type": "section",
                "expand": True,
                "text": {
                    "type": "mrkdwn",
                    "text": f"💬 {escape_mrkdwn(state.text_preview)}",
                },
            }
        )

    # Cancel button — present only while the turn is running (non-terminal).
    # action_id="cancel_turn"; no `value` field (D-01: rejected token-in-value).
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "cancel_turn",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "style": "danger",
                }
            ],
        }
    )

    return blocks
