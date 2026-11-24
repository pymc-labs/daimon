"""Host-issued routine provenance, propagated only into signed chat credentials."""

from collections.abc import Mapping
from contextvars import ContextVar
from typing import NamedTuple
from uuid import UUID


class WorkflowExecution(NamedTuple):
    """A workflow run's routine step's grant. A routine fire's is a plain
    `(routine_id, execution_id)`; this names the run instead of a routine."""

    run_id: UUID
    execution_id: UUID


current_intake_execution: ContextVar[tuple[UUID, UUID] | None] = ContextVar(
    "lead_intake_execution", default=None
)

#: Vault metadata of a credential's grant: its holder (a routine or a workflow run) and id.
_ROUTINE_KEY, _RUN_KEY, _GRANT_KEY = (
    "daimon_routine_id",
    "daimon_workflow_run_id",
    "daimon_routine_execution",
)
EXECUTION_METADATA_KEYS = frozenset({_ROUTINE_KEY, _RUN_KEY, _GRANT_KEY})


def execution_metadata(execution: tuple[UUID, UUID]) -> dict[str, str]:
    holder = _RUN_KEY if isinstance(execution, WorkflowExecution) else _ROUTINE_KEY
    return {holder: str(execution[0]), _GRANT_KEY: str(execution[1])}


def execution_from_metadata(metadata: Mapping[str, str]) -> tuple[UUID, UUID] | None:
    """The grant `execution_metadata` recorded, or None when there is none."""
    grant = metadata.get(_GRANT_KEY)
    if not grant:
        return None
    if run := metadata.get(_RUN_KEY):
        return WorkflowExecution(UUID(run), UUID(grant))
    return UUID(metadata[_ROUTINE_KEY]), UUID(grant)
