"""Driver-owned records; the host supplies transactional persistence.

No process-local storage is selected by the production factory. The scripted
fake supplies a memory transaction for offline probes. Persist the complete
Records transaction before acknowledging writes, and serialize transactions
across workers for the same provider account.
"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from mux.contracts.events import Event
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.receipts import SendReceipt
from mux.contracts.resources import Agent, Environment, Session, SessionSpec
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini.transport import Object


@dataclass
class SessionRecord:
    owner: tuple[str, str]
    session: Session
    spec: SessionSpec
    model: ModelRef
    request: Object
    current: str | None = None
    environment_id: str | None = None
    root: str | None = None
    workspace_expires_at: datetime | None = None
    interactions: dict[str, str] = field(default_factory=lambda: {})
    events: dict[str, Event] = field(default_factory=lambda: {})
    observations: dict[str, UsageObservation] = field(default_factory=lambda: {})
    native_updated: dict[str, datetime] = field(default_factory=lambda: {})
    observation_history: dict[tuple[str, int], UsageObservation] = field(default_factory=lambda: {})
    unknown_delivery: bool = False


@dataclass
class Records:
    agents: dict[str, Agent] = field(default_factory=lambda: {})
    environments: dict[str, Environment] = field(default_factory=lambda: {})
    sessions: dict[str, SessionRecord] = field(default_factory=lambda: {})
    sends: dict[tuple[str, str, str], tuple[str, str, SendReceipt]] = field(
        default_factory=lambda: {}
    )
    creations: dict[tuple[str, str, str], tuple[str, str, str]] = field(default_factory=lambda: {})


class Storage(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[Records]:
        """Commit atomically on success, rollback on exception; serialize writers."""
        ...


def owner(scope: Scope) -> tuple[str, str]:
    return scope.tenant_id, scope.account_id
