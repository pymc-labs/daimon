"""RoutinesPanelState — per-View state for /routines.

Pure logic only: a glyph/color reducer and two frozen-ish dataclasses.
No I/O, no clock, no DB.
"""

from __future__ import annotations

import dataclasses
import uuid

from daimon.adapters.discord import theme
from daimon.core.routines import Glyph, derive_glyph
from daimon.core.stores.domain import RoutineRow

__all__ = [
    "Glyph",
    "RoutineEntry",
    "RoutinesPanelState",
    "derive_state",
    "picker_label",
    "state_label",
]

_COLOR: dict[Glyph, int] = {
    "⏸": theme.COLOR_PAUSED,
    "⏳": theme.COLOR_BLURPLE,
    "❌": theme.COLOR_RED,
    "✅": theme.COLOR_GREEN,
}


def derive_state(row: RoutineRow) -> tuple[Glyph, int]:
    """`derive_glyph`'s state glyph and the embed accent color that goes with it."""
    glyph = derive_glyph(row)
    return glyph, _COLOR[glyph]


def picker_label(row: RoutineRow) -> str:
    """Picker label: trigger_message[:60] or a hex-id fallback for blank messages."""
    stripped = row.trigger_message.strip()
    if not stripped:
        return f"routine {row.id.hex[:8]}"
    return stripped[:60]


def state_label(glyph: Glyph) -> str:
    """Human label for a state glyph (used in the embed Status field)."""
    return {
        "⏸": "Paused",
        "⏳": "Not run yet",
        "❌": "Error recorded",
        "✅": "No errors",
    }[glyph]


@dataclasses.dataclass(frozen=True)
class RoutineEntry:
    routine: RoutineRow
    agent_name: str
    glyph: Glyph
    color: int
    label: str


@dataclasses.dataclass
class RoutinesPanelState:
    rows: list[RoutineEntry]
    selected: RoutineEntry | None
    over_cap_count: int
    agent_name_map: dict[str, str]

    @classmethod
    def initial(
        cls,
        *,
        rows: list[RoutineEntry],
        over_cap_count: int,
        agent_name_map: dict[str, str],
    ) -> RoutinesPanelState:
        return cls(
            rows=rows,
            selected=(rows[0] if rows else None),
            over_cap_count=over_cap_count,
            agent_name_map=agent_name_map,
        )

    def select(self, routine_id: uuid.UUID) -> None:
        for entry in self.rows:
            if entry.routine.id == routine_id:
                self.selected = entry
                return
        # Unknown id: no-op (routine could have been deleted between renders).
