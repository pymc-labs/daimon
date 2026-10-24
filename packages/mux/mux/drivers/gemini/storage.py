"""Driver-owned records; the host supplies transactional persistence.

No process-local storage is selected by the production factory. The scripted
fake supplies a memory transaction for offline probes. Persist the complete
Records transaction before acknowledging writes, and serialize transactions
across workers for the same provider account.
"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, cast

from mux.contracts.events import Event
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.receipts import DeletionReceipt, SendReceipt
from mux.contracts.resources import (
    Agent,
    Artifact,
    Environment,
    Session,
    SessionSpec,
    Skill,
    SkillUpload,
    SkillVersion,
)
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini.skill_deployment import GCSBundleDeployment
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
    interaction_models: dict[str, ModelRef] = field(default_factory=dict[str, ModelRef])
    native_updated: dict[str, datetime] = field(default_factory=lambda: {})
    observation_history: dict[tuple[str, int], UsageObservation] = field(default_factory=lambda: {})
    unknown_delivery: bool = False
    max_input_tokens: int | None = None
    # Native (Gemini-safe) mcp_server name -> declared connection name.
    mcp_server_names: dict[str, str] = field(default_factory=dict[str, str])
    # Interaction id -> SHA-256 of each storage-mount header sent for it. Lets a
    # later turn or a restart clear that interaction's echo; never the token.
    mount_echo_digests: dict[str, list[str]] = field(default_factory=dict[str, list[str]])


@dataclass
class SkillRecord:
    owner: tuple[str, str]
    account_scope_id: str
    skill: Skill
    bundles: dict[str, SkillUpload] = field(default_factory=dict[str, SkillUpload])
    versions: dict[str, SkillVersion] = field(default_factory=dict[str, SkillVersion])
    deployments: dict[str, GCSBundleDeployment] = field(
        default_factory=dict[str, GCSBundleDeployment]
    )


@dataclass
class ArtifactRecord:
    artifact: Artifact
    digest: str
    body: bytes | None = None


@dataclass
class Records:
    agents: dict[str, Agent] = field(default_factory=lambda: {})
    environments: dict[str, Environment] = field(default_factory=lambda: {})
    sessions: dict[str, SessionRecord] = field(default_factory=lambda: {})
    skills: dict[str, SkillRecord] = field(default_factory=dict[str, SkillRecord])
    artifacts: dict[str, ArtifactRecord] = field(default_factory=dict[str, ArtifactRecord])
    artifact_pages: dict[str, tuple[str, str | None, str, tuple[str, ...]]] = field(
        default_factory=dict[str, tuple[str, str | None, str, tuple[str, ...]]]
    )
    deletions: dict[tuple[str, str, str], tuple[str, str, DeletionReceipt]] = field(
        default_factory=dict[tuple[str, str, str], tuple[str, str, DeletionReceipt]]
    )
    sends: dict[tuple[str, str, str], tuple[str, str, SendReceipt]] = field(
        default_factory=lambda: {}
    )
    creations: dict[tuple[str, str, str], tuple[str, str, str]] = field(default_factory=lambda: {})


class Storage(Protocol):
    def transaction(self, *, after_dispatch: bool = False) -> AbstractAsyncContextManager[Records]:
        """Serialize atomic writes; allow post-dispatch outcome acquisition to wait.

        The host's enclosing send timeout (120s by default, including pre-dispatch
        work and POST time) or earlier request cancellation still bounds that wait.
        """
        ...


def read_records(storage: Storage) -> AbstractAsyncContextManager[Records]:
    """Records for a pure read: a committed snapshot where the storage offers `read()`.

    A storage may provide `read()`: the last committed Records, taken without the
    write fence and never saved, so listing usage or events never waits behind a
    writer for the same account. Without it, the write transaction serves the read.
    Mutating what it yields persists nothing.
    """
    reader = getattr(storage, "read", None)
    if callable(reader):
        return cast(AbstractAsyncContextManager[Records], reader())
    return storage.transaction()


def owner(scope: Scope) -> tuple[str, str]:
    return scope.tenant_id, scope.account_id
