"""Port protocols: the surface a driver implements and the host calls.

Every mutating call takes `key`, the operation key the state store
deduplicates on, and `expected` (a revision) where a lost update is
possible. Each port takes the caller's `Scope`; a reference from another
tenant raises `ScopeViolation`. Provider SDK types never appear here.

Native features go through `ManagedAgents.extension`, which returns a typed
port from the bottom of this module. There is no raw client attribute.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Protocol

from pydantic import JsonValue

from mux.contracts.actions import InputEvent, UserMessage
from mux.contracts.admission import Admission
from mux.contracts.config import ConfigRevision
from mux.contracts.events import Event
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import (
    ModelRef,
    Page,
    PageRequest,
    Provider,
    ResourceRef,
    Revision,
    Scope,
    SkillRef,
)
from mux.contracts.profile import Profile
from mux.contracts.receipts import (
    CancelReceipt,
    DeletionReceipt,
    Operation,
    RestoreReceipt,
    SendReceipt,
    StopObservation,
    UpdateReceipt,
)
from mux.contracts.resources import (
    Agent,
    AgentFilter,
    AgentPatch,
    AgentSpec,
    AgentThread,
    Artifact,
    CredentialBinding,
    CredentialInfo,
    Environment,
    EnvironmentFilter,
    EnvironmentPatch,
    EnvironmentSpec,
    ExportRequirements,
    Memory,
    MemoryStore,
    ModelAdmission,
    ModelInfo,
    ProjectionSnapshot,
    ResourceBinding,
    Session,
    SessionExport,
    SessionFilter,
    SessionSpec,
    Skill,
    SkillUpload,
    SkillVersion,
    UpdatePlan,
    Vault,
)
from mux.contracts.usage import UsageObservation


class Agents(Protocol):
    async def create(self, scope: Scope, spec: AgentSpec, *, key: str) -> Agent: ...
    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Agent: ...
    async def list(
        self, scope: Scope, *, filters: AgentFilter, page: PageRequest
    ) -> Page[Agent]: ...
    async def update(
        self, scope: Scope, ref: ResourceRef, patch: AgentPatch, *, expected: Revision, key: str
    ) -> Agent: ...
    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation: ...
    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt: ...


class Environments(Protocol):
    async def create(self, scope: Scope, spec: EnvironmentSpec, *, key: str) -> Environment: ...
    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Environment: ...
    async def list(
        self, scope: Scope, *, filters: EnvironmentFilter, page: PageRequest
    ) -> Page[Environment]: ...
    async def update(
        self,
        scope: Scope,
        ref: ResourceRef,
        patch: EnvironmentPatch,
        *,
        expected: Revision,
        key: str,
    ) -> Environment: ...
    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation: ...
    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt: ...


class Sessions(Protocol):
    async def create(
        self, scope: Scope, spec: SessionSpec, *, key: str, initial: UserMessage | None = None
    ) -> Session: ...
    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Session: ...
    async def list(
        self, scope: Scope, *, filters: SessionFilter, page: PageRequest
    ) -> Page[Session]: ...
    async def plan_update(
        self, scope: Scope, ref: ResourceRef, desired: SessionSpec
    ) -> UpdatePlan: ...
    async def apply_update(
        self, scope: Scope, plan: UpdatePlan, *, expected: Revision, key: str
    ) -> UpdateReceipt: ...
    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation: ...
    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt: ...
    async def export(
        self, scope: Scope, ref: ResourceRef, *, requested: ExportRequirements, key: str
    ) -> SessionExport: ...
    async def restore(
        self,
        scope: Scope,
        export: SessionExport,
        target: SessionSpec,
        *,
        accept_losses: frozenset[str],
        key: str,
    ) -> RestoreReceipt: ...
    async def migrate(
        self, scope: Scope, ref: ResourceRef, target: ConfigRevision, *, expected: int, key: str
    ) -> Session:
        """Move an existing thread to another backend.

        Not supported in this release: every implementation raises
        `MigrationUnsupported`. A backend change applies to new threads only.
        `expected` is the binding generation the caller last saw.
        """
        ...


class Events(Protocol):
    async def send(
        self,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None = None,
    ) -> SendReceipt: ...
    def stream(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        after: str | None = None,
        previews: bool = False,
    ) -> AsyncIterator[Event]: ...
    async def list(
        self, scope: Scope, session: ResourceRef, *, page: PageRequest
    ) -> Page[Event]: ...
    async def reconcile(self, scope: Scope, session: ResourceRef) -> ProjectionSnapshot: ...
    async def cancel(
        self, scope: Scope, session: ResourceRef, *, turn_id: str, key: str
    ) -> CancelReceipt: ...
    async def wait_stopped(
        self, scope: Scope, receipt: CancelReceipt, *, deadline: datetime
    ) -> StopObservation: ...


class Artifacts(Protocol):
    async def upload(
        self,
        scope: Scope,
        body: AsyncIterator[bytes],
        *,
        filename: str,
        media_type: str,
        key: str,
    ) -> Artifact: ...
    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Artifact: ...
    async def list(
        self, scope: Scope, session: ResourceRef, *, page: PageRequest, turn_id: str | None = None
    ) -> Page[Artifact]: ...
    def download(self, scope: Scope, ref: ResourceRef) -> AsyncIterator[bytes]: ...
    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt: ...


class Skills(Protocol):
    """Skills. A bundle travels inline with `create` or `publish_version`, in one request."""

    async def create(self, scope: Scope, bundle: SkillUpload, *, key: str) -> Skill: ...
    async def publish_version(
        self, scope: Scope, skill_id: str, bundle: SkillUpload, *, key: str
    ) -> SkillRef: ...
    async def retrieve(self, scope: Scope, skill_id: str) -> Skill: ...
    async def list(self, scope: Scope, *, page: PageRequest) -> Page[Skill]: ...
    async def delete(self, scope: Scope, skill_id: str, *, key: str) -> DeletionReceipt: ...


class Models(Protocol):
    async def list(self, scope: Scope, provider: Provider) -> tuple[ModelInfo, ...]: ...
    async def validate(self, scope: Scope, model: ModelRef) -> ModelAdmission: ...


class Usage(Protocol):
    async def reconcile(
        self, scope: Scope, session: ResourceRef
    ) -> tuple[UsageObservation, ...]: ...
    async def list(
        self, scope: Scope, session: ResourceRef, *, page: PageRequest
    ) -> Page[UsageObservation]: ...


class ManagedAgents(Protocol):
    """One driver: the core ports plus capabilities, admission and extensions."""

    @property
    def agents(self) -> Agents: ...
    @property
    def environments(self) -> Environments: ...
    @property
    def sessions(self) -> Sessions: ...
    @property
    def events(self) -> Events: ...
    @property
    def artifacts(self) -> Artifacts: ...
    @property
    def skills(self) -> Skills: ...
    @property
    def models(self) -> Models: ...
    @property
    def usage(self) -> Usage: ...

    def capabilities(self) -> Profile:
        """The profile this driver runs."""
        ...

    def admit(self, config: ConfigRevision) -> Admission:
        """`mux.contracts.admission.admit` against this driver's profile."""
        ...

    def extension[T](self, port: type[T], *, namespace: str, version: int) -> T:
        """The typed extension port at `namespace`@`version`.

        Raises `UnsupportedCapability` when the profile does not offer the
        namespace and `ExtensionVersionError` when it offers another version.
        """
        ...


# Extension ports. Namespaces are listed in `mux.contracts.extensions`.


class Steering(Protocol):
    """`openai.steer`: add input to the active turn."""

    async def steer(
        self,
        scope: Scope,
        session: ResourceRef,
        message: UserMessage,
        *,
        active_turn: str,
        key: str,
    ) -> SendReceipt: ...


class SessionResources(Protocol):
    """`anthropic.session_resources`: change what is mounted into a live session."""

    async def list(self, scope: Scope, session: ResourceRef) -> tuple[ResourceBinding, ...]: ...
    async def add(
        self, scope: Scope, session: ResourceRef, resource: ResourceBinding, *, key: str
    ) -> UpdateReceipt: ...
    async def remove(
        self, scope: Scope, session: ResourceRef, resource_id: str, *, key: str
    ) -> UpdateReceipt: ...
    async def rotate_repo_token(
        self,
        scope: Scope,
        session: ResourceRef,
        resource_id: str,
        credential_ref: str,
        *,
        key: str,
    ) -> UpdateReceipt: ...


class Vaults(Protocol):
    """`anthropic.vaults`, `openai.vaults`: provider-held credentials."""

    async def ensure(self, scope: Scope, name: str, *, key: str) -> Vault: ...
    async def list(self, scope: Scope, *, page: PageRequest) -> Page[Vault]: ...
    async def credentials(self, scope: Scope, vault: ResourceRef) -> tuple[CredentialInfo, ...]: ...
    async def put(
        self,
        scope: Scope,
        vault: ResourceRef,
        credential: CredentialBinding,
        *,
        expected: Revision | None,
        key: str,
    ) -> CredentialInfo: ...
    async def remove(
        self, scope: Scope, vault: ResourceRef, credential_id: str, *, key: str
    ) -> Operation: ...
    async def archive(self, scope: Scope, vault: ResourceRef, *, key: str) -> Operation: ...


class MemoryStores(Protocol):
    """`anthropic.memory_stores`: shared native memory."""

    async def list(self, scope: Scope, *, page: PageRequest) -> Page[MemoryStore]: ...
    async def retrieve(self, scope: Scope, store: ResourceRef) -> MemoryStore: ...
    async def create(
        self, scope: Scope, name: str, description: str, *, key: str
    ) -> ResourceRef: ...
    async def memories(
        self, scope: Scope, store: ResourceRef, *, path_prefix: str, page: PageRequest
    ) -> Page[Memory]: ...
    async def read(self, scope: Scope, store: ResourceRef, memory_id: str) -> Memory: ...
    async def write(
        self,
        scope: Scope,
        store: ResourceRef,
        path: str,
        content: str,
        *,
        expected: Revision | None,
        key: str,
    ) -> Memory: ...
    async def archive(self, scope: Scope, store: ResourceRef, *, key: str) -> Operation: ...
    async def delete(self, scope: Scope, store: ResourceRef, *, key: str) -> DeletionReceipt: ...


class SkillVersions(Protocol):
    """`anthropic.skills_versions`: a skill's version history."""

    async def versions(
        self, scope: Scope, skill_id: str, *, page: PageRequest
    ) -> Page[SkillVersion]: ...
    def download(self, scope: Scope, ref: SkillRef) -> AsyncIterator[bytes]: ...
    async def delete_version(self, scope: Scope, ref: SkillRef, *, key: str) -> DeletionReceipt: ...


class Multiagent(Protocol):
    """`anthropic.multiagent`: subagent threads inside a session."""

    async def threads(self, scope: Scope, session: ResourceRef) -> tuple[AgentThread, ...]: ...
    def stream(
        self, scope: Scope, session: ResourceRef, thread_id: str
    ) -> AsyncIterator[Event]: ...


class EnvironmentsFork(Protocol):
    """`anthropic.environments_fork`: a new environment from an existing one."""

    async def fork(
        self, scope: Scope, source: ResourceRef, config: ExtensionConfig, *, key: str
    ) -> Environment: ...


class PlatformExport(Protocol):
    """`anthropic.platform_export`: an operator dump of native state.

    Returns the provider's own JSON, by design: the feature is exporting
    native state. It still runs under a scope; no client is exposed.
    """

    async def export(
        self, scope: Scope, *, resource_kinds: frozenset[str]
    ) -> Mapping[str, JsonValue]: ...
