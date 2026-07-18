"""RoutinesPanelState — per-modal state for /routines.

Pure logic only: a glyph reducer and two frozen dataclasses.
No I/O, no clock, no DB, no slack_sdk.

Port of discord/routines_panel/state.py with the theme-color tuple dropped
(Slack uses emoji + text, not embed accent colors).
"""

from __future__ import annotations

import dataclasses
from typing import Literal

from daimon.core.stores.domain import RoutineRow

__all__ = [
    "Glyph",
    "RoutineEntry",
    "RoutinesPanelState",
    "derive_glyph",
    "picker_label",
    "state_label",
]

Glyph = Literal["⏸", "⏳", "❌", "✅"]


def derive_glyph(row: RoutineRow) -> Glyph:
    """Single-glyph precedence: Paused > Never-run > Error > Success.

    ``record_result`` clears ``last_error`` on success, so
    ``last_error is not None`` always reflects the most recent run.
    """
    if not row.enabled:
        return "⏸"
    if row.last_fired_at is None:
        return "⏳"
    if row.last_error is not None:
        return "❌"
    return "✅"


def picker_label(row: RoutineRow) -> str:
    """Picker label: trigger_message[:60] or a hex-id fallback for blank messages."""
    stripped = row.trigger_message.strip()
    if not stripped:
        return f"routine {row.id.hex[:8]}"
    return stripped[:60]


def state_label(glyph: Glyph) -> str:
    """Human label for a state glyph (used in section text)."""
    return {
        "⏸": "Paused",
        "⏳": "Never run",
        "❌": "Errored",
        "✅": "Success",
    }[glyph]


@dataclasses.dataclass(frozen=True)
class RoutineEntry:
    """Decorated view-model for one routine row — no color (Slack has no embed accents)."""

    routine: RoutineRow
    agent_name: str
    glyph: Glyph
    label: str


@dataclasses.dataclass
class RoutinesPanelState:
    """State for the /routines panel modal."""

    rows: list[RoutineEntry]
    over_cap_count: int
    agent_name_map: dict[str, str]
