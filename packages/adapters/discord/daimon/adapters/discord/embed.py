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
    SUMMARY_GAP,
    format_duration,
    format_headline,
    format_summary,
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
# Discord caps an embed footer at 2,048 characters.
_FOOTER_MAX_CHARS = 2048


def _escape_markdown(text: str) -> str:
    """Escape Discord markdown so truncated preview text renders literally
    (a clipped draft can otherwise leave unclosed code fences / bold markers)."""
    for char in r"\`*_~|>[]()#":
        text = text.replace(char, f"\\{char}")
    return text


def to_embed_data(state: EmbedState, *, now: float | None = None) -> EmbedData:
    """Render EmbedState into an EmbedData output shape.

    now: current monotonic time (pass time.monotonic() from caller).
    Terminal phases put name, time, cost and money left on one footer line.
    """
    color = _PHASE_COLOR[state.phase]

    if state.phase in _TERMINAL_PHASES:
        # Terminal turns drop the activity trail. The coloured edge signals
        # outcome; the footer carries the numbers on one quiet line.
        elapsed = now - state.started_at if now is not None else 0
        numbers = format_summary(
            agent_name=None, elapsed_seconds=elapsed, cost=state.cost_str, left=state.balance_str
        )
        # The name gives way so the numbers always fit the footer.
        room = _FOOTER_MAX_CHARS - len(numbers) - len(SUMMARY_GAP)
        name = state.agent_name
        if len(name) > room:
            name = name[: room - 1] + "…"
        footer = format_summary(
            agent_name=name, elapsed_seconds=elapsed, cost=state.cost_str, left=state.balance_str
        )
        description = ""
        title = ""
        notice_text: str | None = None
        if state.phase is TurnPhase.ERROR:
            title = "Something went wrong."
            description = "Mention me to try again."
            if state.notice:
                next_marker = "**Next:** "
                if next_marker in state.notice:
                    extracted = state.notice.split(next_marker, 1)[1].split("\n", 1)[0].strip()
                    if extracted:
                        description = extracted
                    notice_text = "\n".join(
                        line
                        for line in state.notice.splitlines()
                        if not line.startswith(next_marker)
                    )
                else:
                    notice_text = state.notice
        return EmbedData(
            phase=state.phase,
            title=title,
            description=description,
            color=color,
            footer=footer,
            details=None,
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
        details=(
            "\n".join(f"`{line.replace('`', "'")}`" for line in state.tool_lines)
            if state.tool_lines
            else None
        ),
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
