"""What one fire reads: the facts of what fires, never the row they are stored in."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from daimon.core.stores.domain import RoutineDestinationKind, RoutineRow


class RoutineFacts(Protocol):
    """The fields a fire and its helpers read; a `RoutineRow` and a `Subject` both have them."""

    @property
    def id(self) -> uuid.UUID: ...
    @property
    def tenant_id(self) -> uuid.UUID: ...
    @property
    def name(self) -> str | None: ...
    @property
    def created_by_user_id(self) -> str | None: ...
    @property
    def agent_name(self) -> str: ...
    @property
    def agent_id(self) -> str: ...
    @property
    def cron_expr(self) -> str: ...
    @property
    def timezone(self) -> str: ...
    @property
    def trigger_message(self) -> str: ...
    @property
    def destination_kind(self) -> RoutineDestinationKind | None: ...
    @property
    def destination_id(self) -> str | None: ...
    @property
    def channel_id(self) -> str | None: ...
    @property
    def source_provenance_known(self) -> bool: ...
    @property
    def source_channel_id(self) -> str | None: ...
    @property
    def source_thread_id(self) -> str | None: ...


@dataclass(frozen=True)
class Subject:
    """One fire's input, built from a routine row (`routine_subject`).

    `id` tags usage, ops events and the controls; `created_by_user_id` is the owner;
    `channel_id` the channel the fire is billed to; `cron_expr` and `timezone` the schedule
    the controls name; `source_*` where it was made.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    created_by_user_id: str | None
    agent_id: str
    agent_name: str
    cron_expr: str
    timezone: str
    trigger_message: str
    name: str | None = None
    destination_kind: RoutineDestinationKind | None = None
    destination_id: str | None = None
    channel_id: str | None = None
    source_provenance_known: bool = False
    source_channel_id: str | None = None
    source_thread_id: str | None = None
    #: What a DM fire's relay checks is unedited (ISO 8601): the routine's `updated_at`.
    revision: str | None = None
    #: Whether the routine is enabled. A paused one signs no `rev`.
    enabled: bool = True


def routine_subject(row: RoutineRow) -> Subject:
    """The fire input for a claimed routine row."""
    return Subject(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        agent_name=row.agent_name,
        created_by_user_id=row.created_by_user_id,
        name=row.name,
        cron_expr=row.cron_expr,
        timezone=row.timezone,
        trigger_message=row.trigger_message,
        destination_kind=row.destination_kind,
        destination_id=row.destination_id,
        channel_id=row.channel_id,
        source_provenance_known=row.source_provenance_known,
        source_channel_id=row.source_channel_id,
        source_thread_id=row.source_thread_id,
        revision=row.updated_at.isoformat(),
        enabled=row.enabled,
    )
