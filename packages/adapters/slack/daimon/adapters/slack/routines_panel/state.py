"""RoutinesPanelState — per-modal state for /routines.

Pure logic only: two frozen dataclasses. The glyph and label rules live in
``daimon.core.routines``, shared with the other chat panels.
"""

from __future__ import annotations

import dataclasses
import uuid

from daimon.core.routines import Glyph
from daimon.core.stores.domain import RoutineRow
from daimon.core.web_links import TenantLinks

__all__ = ["RoutineEntry", "RoutinesPanelState"]


@dataclasses.dataclass(frozen=True)
class RoutineEntry:
    """Decorated view-model for one routine row — no color (Slack has no embed accents)."""

    routine: RoutineRow
    agent_name: str
    glyph: Glyph
    label: str
    #: The workflow it runs as since an admin moved it; it changes there.
    workflow_id: uuid.UUID | None = None


@dataclasses.dataclass
class RoutinesPanelState:
    """State for the /routines panel modal."""

    rows: list[RoutineEntry]
    over_cap_count: int
    agent_name_map: dict[str, str]
    links: TenantLinks | None = None
    #: Whether the viewer is an admin, who may move a routine to a workflow.
    admin: bool = False
