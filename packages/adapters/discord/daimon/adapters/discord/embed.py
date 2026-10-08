"""Pure embed state machine for Discord turn UX.

Converts EmbedEvents and the turn's content into EmbedState and EmbedData with
zero I/O dependencies. The in-progress card's words come from
`daimon.core.turn.status_lines`, shared with Slack; this module adds the
Discord markup and the phase colors. No discord or anthropic imports.
"""

from __future__ import annotations

from dataclasses import dataclass

from daimon.adapters.discord.theme import (
    COLOR_GREEN,
    COLOR_IN_PROGRESS,
    COLOR_RED,
)
from daimon.core.turn.card_state import (
    CardEvent as _CardEvent,
)
from daimon.core.turn.card_state import (
    CardState as EmbedState,
)
from daimon.core.turn.card_state import (
    TurnPhase as TurnPhase,
)
from daimon.core.turn.card_state import (
    update as update,
)
from daimon.core.turn.card_state import (
    update_activity as update_activity,
)
from daimon.core.turn.notices import TerminationNotice, fit_notice
from daimon.core.turn.status_lines import (
    format_duration,
    format_headline,
)

EmbedEvent = _CardEvent

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
    details: str | None = None
    notice: str | None = None


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
_NOTICE_MAX_CHARS = 950


def _escape_markdown(text: str) -> str:
    """Escape Discord markdown so truncated preview text renders literally
    (a clipped draft can otherwise leave unclosed code fences / bold markers)."""
    for char in r"\`*_~|>[]()#":
        text = text.replace(char, f"\\{char}")
    return text


def _fmt_tokens(n: int) -> str:
    """Humanize a token count: <1000 verbatim, else one-decimal k with trailing
    ``.0`` stripped (``320`` -> ``"320"``, ``1500`` -> ``"1.5k"``, ``12000`` -> ``"12k"``)."""
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"


def to_embed_data(state: EmbedState, *, now: float | None = None) -> EmbedData:
    """Render EmbedState into an EmbedData output shape.

    now: current monotonic time (pass time.monotonic() from caller).
    Terminal phases put time, cost, tokens and balance in Details.
    """
    color = _PHASE_COLOR[state.phase]

    if state.phase in _TERMINAL_PHASES:
        # Terminal turns drop the activity trail. The coloured edge signals
        # outcome; metrics remain visible together in Details.
        elapsed = int(now - state.started_at) if now is not None else 0
        tokens = f"{_fmt_tokens(state.usage_in)} in / {_fmt_tokens(state.usage_out)} out"
        parts = [f"Time: {elapsed}s"]
        if state.cost_str is not None:
            parts.append(f"Cost: {state.cost_str}")
        parts.append(f"Tokens: {tokens}")
        if state.balance_str is not None:
            parts.append(f"Balance: {state.balance_str}")
        description = ""
        title = ""
        details = "\n".join(parts)
        notice_text: str | None = None
        if state.phase is TurnPhase.ERROR:
            footer = state.agent_name
            title = "Something went wrong."
            description = "Mention me to try again."
            if state.notice:
                next_marker = "**Next:** "
                if next_marker in state.notice:
                    description = state.notice.split(next_marker, 1)[1].split("\n", 1)[0]
                    notice_text = "\n".join(
                        line
                        for line in state.notice.splitlines()
                        if not line.startswith(next_marker)
                    )
                else:
                    notice_text = state.notice
        else:
            footer = state.agent_name
        return EmbedData(
            phase=state.phase,
            title=title,
            description=description,
            color=color,
            footer=footer,
            details=details,
            notice=notice_text,
        )

    # In progress: one embed, the headline, the tool lines, then the latest draft.
    elapsed_seconds = now - state.started_at if now is not None and state.started_at else None
    title = format_headline(
        is_working=state.phase is TurnPhase.TOOL_RUNNING,
        elapsed_seconds=elapsed_seconds,
        bold=lambda word: word,
    )
    sections: list[str] = []
    if state.text_preview:
        sections.append(f"> {_escape_markdown(state.text_preview)}")
    return EmbedData(
        phase=state.phase,
        title=title,
        description="\n\n".join(sections),
        color=color,
        footer=format_duration(elapsed_seconds) if elapsed_seconds is not None else None,
        details="\n".join(state.tool_lines) if state.tool_lines else None,
    )


def format_termination_notice(notice: TerminationNotice) -> str:
    """Draw the core notice below the ERROR card's title, in Discord markdown."""
    lines = [_escape_markdown(notice.cause)]
    if (work := notice.work_line(lambda name: f"`{name.replace('`', '')}`")) is not None:
        lines.append(work)
    lines.append(notice.survived)
    lines.append(f"**Next:** {notice.next_step}")
    tail = f"`rid: {notice.request_id}`" if notice.request_id is not None else None
    return fit_notice(lines, tail=tail, limit=_NOTICE_MAX_CHARS)
