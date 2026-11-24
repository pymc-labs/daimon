"""What one fire reads: the facts of what fires, never the row they are stored in."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from daimon.core.stores.domain import RoutineDestinationKind, RoutineRow
from daimon.core.stores.workflows import WorkflowRow
from daimon.core.workflows.spec import RoutineNode, ScheduleTrigger, Spec


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
    """One fire's input, built from a routine row (`routine_subject`) or a workflow run's routine
    step (`workflow_subject`, whose `id` is the workflow's and `workflow_run_id` the run's).

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
    workflow_run_id: uuid.UUID | None = None


def tier_ids(facts: RoutineFacts) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """The fire's own feature tier and tag, as `(routine_id, workflow_id)`."""
    if isinstance(facts, Subject) and facts.workflow_run_id is not None:
        return None, facts.id
    return facts.id, None


def workflow_subject(
    workflow: WorkflowRow,
    spec: Spec,
    node: RoutineNode,
    *,
    run_id: uuid.UUID,
    owner: str | None,
    agent_id: str,
) -> Subject:
    """The fire input for a run's routine step: `owner` is the owner's platform id, the budget
    channel the workflow's home and the schedule its first schedule trigger's."""
    schedule = next((t for t in spec.triggers or [] if isinstance(t, ScheduleTrigger)), None)
    made_in = workflow.made_in
    to = node.to
    return Subject(
        id=workflow.id,
        tenant_id=workflow.tenant_id,
        created_by_user_id=owner,
        agent_id=agent_id,
        agent_name=node.agent,
        cron_expr=(schedule.cron if schedule else None) or "",
        timezone=(schedule.timezone if schedule else None) or spec.timezone or "UTC",
        trigger_message=node.prompt,
        name=workflow.name,
        destination_kind=to.kind if to else None,
        destination_id=(owner if to.kind == "dm" else to.id) if to else None,
        channel_id=workflow.home_channel_id,
        source_provenance_known=bool(made_in and made_in["known"]),
        source_channel_id=made_in["channel_id"] if made_in else None,
        source_thread_id=made_in["thread_id"] if made_in else None,
        workflow_run_id=run_id,
    )


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
    )
