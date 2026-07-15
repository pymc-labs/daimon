"""Pure embed state machine for Discord turn UX.

Converts EmbedEvents into EmbedState and EmbedData with zero I/O dependencies.
Imports only the stdlib-only `theme` palette — no discord, anthropic, or core
daimon imports.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from daimon.adapters.discord.theme import (
    COLOR_GREEN,
    COLOR_RED,
    COLOR_THINKING,
    COLOR_TOOL_RUNNING,
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
class EmbedState:
    """Accumulated embed state. Immutable — update() returns new instances."""

    phase: TurnPhase = TurnPhase.THINKING
    trail: tuple[TrailEntry, ...] = ()
    agent_name: str = ""
    started_at: float = 0.0
    usage_in: int = 0
    usage_out: int = 0
    cost_str: str | None = None
    text_preview: str | None = None


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

_PHASE_COLOR: dict[TurnPhase, int] = {
    TurnPhase.THINKING: COLOR_THINKING,
    TurnPhase.TOOL_RUNNING: COLOR_TOOL_RUNNING,
    TurnPhase.DONE: COLOR_GREEN,
    TurnPhase.ERROR: COLOR_RED,
}

_PHASE_TITLE: dict[TurnPhase, str] = {
    TurnPhase.THINKING: f"{_EMOJI_BRAIN} thinking",
    TurnPhase.TOOL_RUNNING: f"{_EMOJI_GEAR} running tool",
    TurnPhase.DONE: "complete",
    TurnPhase.ERROR: f"{_EMOJI_CROSS} error",
}

_TERMINAL_PHASES = frozenset({TurnPhase.DONE, TurnPhase.ERROR})

# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

_TRAIL_MAX = 5

_TEXT_PREVIEW_MAX_CHARS = 250


def _escape_markdown(text: str) -> str:
    """Escape Discord markdown so truncated preview text renders literally
    (a 250-char cut can otherwise leave unclosed code fences / bold markers)."""
    for char in r"\`*_~|>[]()#":
        text = text.replace(char, f"\\{char}")
    return text


def update(state: EmbedState, event: EmbedEvent) -> EmbedState:
    """Return a new EmbedState with event applied.

    Trail is capped at _TRAIL_MAX (5) entries — keeps the last 5.
    Phase transitions to match event kind.

    Messages do not enter the trail: the latest agent message text is held in
    ``text_preview`` and rendered as its own bottom embed (cs/daimon style),
    so reasoning is readable instead of truncated to a one-line snippet.
    """
    new_phase = _KIND_PHASE[event.kind]
    if event.kind == "thinking":
        # agent.thinking is a contentless progress ping — MA carries no thinking
        # text to surface. Reflect the phase in the title but add no trail entry;
        # a bare "thinking" line is empty noise next to real tool lines.
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


def to_embed_data(state: EmbedState, *, now: float | None = None) -> EmbedData:
    """Render EmbedState into an EmbedData output shape.

    now: current monotonic time (pass time.monotonic() from caller).
    Footer is only set on terminal phases (DONE, ERROR).
    """
    color = _PHASE_COLOR[state.phase]

    if state.phase in _TERMINAL_PHASES:
        # Terminal turns collapse to ONE line: the activity trail and the phase
        # title drop away, leaving just a summary in the footer. The green/red
        # bar alone signals outcome — DONE shows no checkmark; ERROR keeps the
        # ❌ + its reason so a failed turn still says why.
        elapsed = int(now - state.started_at) if now is not None else 0
        tokens = f"{_fmt_tokens(state.usage_in)} in / {_fmt_tokens(state.usage_out)} out"
        parts = [state.agent_name, f"{elapsed}s", tokens]
        if state.cost_str is not None:
            parts.append(state.cost_str)
        summary = " · ".join(parts)
        if state.phase is TurnPhase.ERROR:
            reason = state.trail[-1].text if state.trail else "error"
            footer = f"{_EMOJI_CROSS} {reason} · {summary}"
        else:
            footer = summary
        return EmbedData(phase=state.phase, title="", description="", color=color, footer=footer)

    # In-progress turns show the title + elapsed + activity trail; no footer yet.
    lines: list[str] = []
    if now is not None and state.started_at:
        lines.append(f"⏱️ {_fmt_elapsed(int(now - state.started_at))}")
    lines.extend(f"{entry.emoji} {entry.text}" for entry in state.trail)
    return EmbedData(
        phase=state.phase,
        title=_PHASE_TITLE[state.phase],
        description="\n".join(lines),
        color=color,
        footer=None,
    )


def _fmt_elapsed(total_seconds: int) -> str:
    """Format elapsed seconds as ``42s`` or ``2m 3s``."""
    total_seconds = max(0, total_seconds)
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}m {seconds}s"


def to_preview_embed_data(state: EmbedState) -> EmbedData | None:
    """Render the latest agent message as its own bottom embed, or None.

    cs/daimon-style text preview: the most recent intermediate message is shown
    in full (up to the 250-char cap applied in ``update``) below the activity
    embed, so reasoning is readable while the turn runs.
    """
    if not state.text_preview:
        return None
    return EmbedData(
        phase=state.phase,
        title="",
        description=f"💬 {_escape_markdown(state.text_preview)}",
        color=_PHASE_COLOR[state.phase],
        footer=None,
    )
