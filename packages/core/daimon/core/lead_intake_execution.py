"""Host-issued routine provenance, propagated only into signed chat credentials."""

from contextvars import ContextVar
from uuid import UUID

current_intake_execution: ContextVar[tuple[UUID, UUID] | None] = ContextVar(
    "lead_intake_execution", default=None
)
