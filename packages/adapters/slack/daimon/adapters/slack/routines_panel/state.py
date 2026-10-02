"""RoutinesPanelState — per-modal state for /routines.

Pure logic only: two frozen dataclasses. The glyph and label rules live in
``daimon.core.routines``, shared with the other chat panels.
"""

from __future__ import annotations

import dataclasses

from daimon.core.routines import Glyph
from daimon.core.stores.domain import RoutineRow

__all__ = ["RoutineEntry", "RoutinesPanelState"]


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
