"""What one fire reads: the facts of what fires, never the row they are stored in."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core.stores.domain import RoutineDestinationKind, RoutineRow


@dataclass(frozen=True)
class Subject:
    """One fire's input. `id` is the routine; `channel_id` the channel it is billed to."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: str
    channel_id: str | None = None
    destination_kind: RoutineDestinationKind | None = None


def routine_subject(row: RoutineRow) -> Subject:
    """The fire input for a claimed routine row."""
    return Subject(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        channel_id=row.channel_id,
        destination_kind=row.destination_kind,
    )
