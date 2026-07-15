"""RoutinesPanelState — per-View state for /routines.

Pure logic only: a glyph/color reducer and two frozen-ish dataclasses.
No I/O, no clock, no DB.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Literal

from daimon.adapters.discord import theme
from daimon.core.stores.domain import RoutineRow

__all__ = [
    "Glyph",
    "RoutineEntry",
    "RoutinesPanelState",
    "derive_state",
    "picker_label",
    "state_label",
]

Glyph = Literal["⏸", "⏳", "❌", "✅"]


def derive_state(row: RoutineRow) -> tuple[Glyph, int]:
    """Single-glyph precedence: Paused > Never-run > Error > Success.

    ``record_result`` writes ``last_error`` and ``last_result_tail`` in one
    UPDATE, clearing ``last_error`` on success — so ``last_error is not None``
    always reflects the most recent run, never a stale older failure.
    """
    if not row.enabled:
        return "⏸", theme.COLOR_PAUSED
    if row.last_fired_at is None:
        return "⏳", theme.COLOR_BLURPLE
    if row.last_error is not None:
        return "❌", theme.COLOR_RED
    return "✅", theme.COLOR_GREEN


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
        "⏳": "Never run",
        "❌": "Errored",
        "✅": "Success",
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
