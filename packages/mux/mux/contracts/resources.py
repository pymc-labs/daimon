"""Resource specs and records: agents, environments, sessions, artifacts, skills.

Specs are what the host asks for; records are what the library tracks. In a
spec, `None` means "not set": the driver sends nothing for it, so the
provider's own default applies. An explicitly empty tuple or mapping is sent
as empty. Patch
types use pydantic's fields-set: a field left out of the constructor is
unchanged, an explicit `None` clears it, and an empty tuple replaces a list
with nothing. Serialize patches with `model_dump(exclude_unset=True)` to keep
that distinction.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, JsonValue

from mux.contracts._base import Contract, FrozenMap
from mux.contracts.actions import RequiredAction
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Provider, ResourceRef, Revision, SkillRef, ThreadRef

Lifecycle = Literal["active", "archived", "deleted"]


class ToolSpec(Contract):
    """A tool the agent may call. `kind` names a provider toolset or a custom tool."""

    name: str
    kind: Literal["builtin", "custom", "mcp_toolset"]
    description: str | None = None
    input_schema: FrozenMap[str, JsonValue] | None = None
    permission: Literal["auto", "ask"] = "auto"


class MCPConnection(Contract):
    """An MCP server. `credential_ref` names a secret in the host's store; never the secret."""

    name: str
    url: str
    transport: Literal["streamable_http", "sse"] = "streamable_http"
    credential_ref: str | None = None
    tool_policy: FrozenMap[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


def _keyed_by_namespace(
    value: Mapping[str, ExtensionConfig] | None,
) -> Mapping[str, ExtensionConfig] | None:
    for namespace, config in (value or {}).items():
        if namespace != config.namespace:
            raise ValueError(f"extension keyed {namespace!r} is {config.namespace!r}")
    return value


ExtensionsByNamespace = Annotated[
    FrozenMap[str, ExtensionConfig] | None, AfterValidator(_keyed_by_namespace)
]
"""Extension configs keyed by their namespace; a patch replaces each one it names."""


class AgentSpec(Contract):
    """An agent to create.

    Provider-specific shapes (Anthropic's toolset configuration, a
    multiagent roster) travel in `extensions` as `anthropic.agent_tools` or
    `anthropic.multiagent` configs, whose schema the driver owns.
    """

    name: str
    description: str | None = None
    model: ModelRef
    system: str | None = None
    tools: tuple[ToolSpec, ...] | None = None
    mcp_servers: tuple[MCPConnection, ...] | None = None
    skills: tuple[SkillRef, ...] | None = None
    extensions: tuple[ExtensionConfig, ...] | None = None
    metadata: FrozenMap[str, str] | None = None


class AgentPatch(Contract):
    name: str | None = None
    description: str | None = None
    model: ModelRef | None = None
    system: str | None = None
    tools: tuple[ToolSpec, ...] | None = None
    mcp_servers: tuple[MCPConnection, ...] | None = None
    skills: tuple[SkillRef, ...] | None = None
    metadata: FrozenMap[str, str | None] | None = None
    extensions: ExtensionsByNamespace = None


class Agent(Contract):
    """An agent as the provider holds it; description and metadata are on `spec`."""

    ref: ResourceRef
    revision: Revision
    spec: AgentSpec
    lifecycle: Lifecycle = "active"
    created_at: datetime
    updated_at: datetime | None = None
    archived_at: datetime | None = None
    native: JsonValue | None = None
    """The provider's own record, filled by the driver and opaque to the host."""


class AgentFilter(Contract):
    name: str | None = None
    include_archived: bool = False
    created_after: datetime | None = None


class WorkspaceSource(Contract):
    """Something placed in the workspace before the first turn."""

    kind: Literal["file", "repository", "skill_bundle"]
    target_path: str
    artifact: ResourceRef | None = None
    repository_url: str | None = None
    ref: str | None = None
    credential_ref: str | None = None


class NetworkPolicy(Contract):
    mode: Literal["unrestricted", "limited", "none"] = "unrestricted"
    allowed_hosts: tuple[str, ...] = ()


class EnvironmentSpec(Contract):
    """An environment to create. `scope` is the provider's visibility scope, if it has one."""

    name: str
    description: str | None = None
    scope: str | None = None
    execution: Literal["hosted", "self_hosted", "none"] = "hosted"
    sources: tuple[WorkspaceSource, ...] | None = None
    network: NetworkPolicy | None = None
    packages: FrozenMap[str, tuple[str, ...]] | None = None
    metadata: FrozenMap[str, str] | None = None
    native_config: ExtensionConfig | None = None


class EnvironmentPatch(Contract):
    name: str | None = None
    description: str | None = None
    scope: str | None = None
    sources: tuple[WorkspaceSource, ...] | None = None
    network: NetworkPolicy | None = None
    packages: FrozenMap[str, tuple[str, ...]] | None = None
    metadata: FrozenMap[str, str | None] | None = None
    native_config: ExtensionConfig | None = None
    extensions: ExtensionsByNamespace = None


class Environment(Contract):
    ref: ResourceRef
    revision: Revision
    spec: EnvironmentSpec
    lifecycle: Lifecycle = "active"
    created_at: datetime
    updated_at: datetime | None = None
    archived_at: datetime | None = None
    native: JsonValue | None = None
    """The provider's own record, filled by the driver and opaque to the host."""


class EnvironmentFilter(Contract):
    name: str | None = None
    include_archived: bool = False


class ResourceBinding(Contract):
    """A resource mounted into a session: an artifact, repository, credential or native store."""

    id: str
    kind: Literal["artifact", "repository", "credential", "native"]
    target_path: str | None = None
    resource: ResourceRef | None = None
    credential_ref: str | None = None
    native: ExtensionConfig | None = None


class SessionSpec(Contract):
    agent: ResourceRef
    agent_revision: Revision
    environment: ResourceRef | None = None
    config_revision: int = Field(ge=0)
    metadata: FrozenMap[str, str] = Field(default_factory=dict[str, str])
    resources: tuple[ResourceBinding, ...] = ()
    state_mode: Literal["continue", "fresh"] = "continue"
    extensions: FrozenMap[str, ExtensionConfig] = Field(default_factory=dict[str, ExtensionConfig])


class Continuity(Contract):
    """What carries over between turns in this thread, and for how long."""

    conversation: Literal["native_session", "interaction_chain", "local_transcript"]
    workspace: Literal["native_reuse", "export_restore", "none"]
    processes: Literal["live", "lost", "unknown", "none"]
    history_expires_at: datetime | None = None
    workspace_expires_at: datetime | None = None
    retention_known: bool = False


class ProviderBinding(Contract):
    """Which provider session backs a thread, at which generation.

    Stored apart from the channel's desired config: changing the config
    affects new threads only, so an existing binding is never rewritten by
    it. `id` is the binding's identity, the one `ContinuityLost.binding_id`
    and the usage adjustment keys name; it is stable across generations.
    `legacy_account_id` is set for a caller-private binding that predates
    shared threads.
    """

    id: str
    thread: ThreadRef
    provider: Provider
    profile: str
    native_refs: FrozenMap[str, str]
    generation: int = Field(ge=0)
    config_revision: int = Field(ge=0)
    legacy_account_id: str | None = None


class Session(Contract):
    ref: ResourceRef
    binding: ProviderBinding
    continuity: Continuity
    requested_revision: Revision
    effective_revision: Revision
    state: Literal["provisioning", "idle", "running", "requires_action", "terminated"]
    active_root_turn: str | None = None
    required_actions: tuple[RequiredAction, ...] = ()
    native: JsonValue | None = None
    """Opaque provider snapshot, filled by the driver for the M0 host codec."""


class SessionFilter(Contract):
    agent: ResourceRef | None = None
    include_archived: bool = False
    created_after: datetime | None = None


class UpdateOperation(Contract):
    kind: Literal["tools", "resources", "agent_revision", "metadata", "native"]
    detail: FrozenMap[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class UpdatePlan(Contract):
    """How a desired session spec would be applied, worked out before anything changes."""

    session: ResourceRef
    expected_revision: Revision
    action: Literal["reuse", "in_place", "next_turn", "replace", "refuse"]
    operations: tuple[UpdateOperation, ...] = ()
    losses: tuple[str, ...] = ()
    unmet: tuple[str, ...] = ()
    extensions: FrozenMap[str, ExtensionConfig] = Field(default_factory=dict[str, ExtensionConfig])


class ExportRequirements(Contract):
    include: frozenset[str] = frozenset({"transcript", "workspace"})
    consistency: Literal["quiescent", "best_effort"] = "quiescent"


class SessionExport(Contract):
    """A file archive plus transcript. Not a runtime snapshot: processes do not travel."""

    source: ResourceRef
    config_revision: int = Field(ge=0)
    journal_cursor: str
    artifacts: tuple[ResourceRef, ...] = ()
    manifest_digest: str
    included: frozenset[str] = frozenset()
    excluded: frozenset[str] = frozenset()
    consistency: Literal["quiescent", "best_effort"]
    losses: tuple[str, ...] = ()


class ProjectionSnapshot(Contract):
    """The reconciled state of a session's journal at a cursor."""

    session: ResourceRef
    cursor: str
    state: Literal["idle", "running", "requires_action", "terminated"]
    active_root_turn: str | None = None
    required_actions: tuple[RequiredAction, ...] = ()
    gaps: tuple[str, ...] = ()
    taken_at: datetime


class Artifact(Contract):
    ref: ResourceRef
    filename: str
    media_type: str
    size_bytes: int | None = Field(default=None, ge=0)
    session: ResourceRef | None = None
    turn_id: str | None = None
    created_at: datetime
    native: JsonValue | None = None
    """The provider's own record, filled by the driver and opaque to the host."""


class SkillUploadFile(Contract):
    """One file of a skill bundle, carried inline."""

    model_config = Contract.model_config | {"ser_json_bytes": "base64", "val_json_bytes": "base64"}

    path: str
    content: bytes
    media_type: str | None = None


class SkillUpload(Contract):
    """A skill bundle sent inline with the create or publish call itself.

    The bytes travel in the same request (multipart where the provider takes
    it), never as a separate upload first.
    """

    model_config = Contract.model_config | {"ser_json_bytes": "base64", "val_json_bytes": "base64"}

    files: tuple[SkillUploadFile, ...]
    display_title: str | None = None


class Skill(Contract):
    """A skill and its newest version."""

    id: str
    display_title: str | None = None
    description: str | None = None
    latest_version: SkillRef | None = None
    source: str | None = None
    metadata: FrozenMap[str, str] | None = None
    created_at: datetime
    updated_at: datetime | None = None
    native: JsonValue | None = None
    """The provider's own record, filled by the driver and opaque to the host."""


class SkillVersion(Contract):
    """One published version; `ref.version` is set."""

    ref: SkillRef
    version: str
    name: str | None = None
    description: str | None = None
    created_at: datetime
    native: JsonValue | None = None
    """The provider's own record, filled by the driver and opaque to the host."""


class ModelInfo(Contract):
    model: ModelRef
    display_name: str | None = None
    context_window: int | None = Field(default=None, ge=1)


class ModelAdmission(Contract):
    model: ModelRef
    admitted: bool
    reason: str | None = None


# Records behind extension ports.


class Vault(Contract):
    ref: ResourceRef
    name: str
    revision: Revision


class CredentialBinding(Contract):
    """A credential to place in a vault, by reference to the host's secret store."""

    name: str
    kind: Literal["static_bearer", "mcp_oauth", "environment"]
    credential_ref: str
    mcp_server_url: str | None = None


class CredentialInfo(Contract):
    """A stored credential's metadata. Never its value."""

    id: str
    name: str
    kind: Literal["static_bearer", "mcp_oauth", "environment"]
    revision: Revision


class MemoryStore(Contract):
    ref: ResourceRef
    name: str
    description: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    archived_at: datetime | None = None
    native: JsonValue | None = None
    """The provider's own record, filled by the driver and opaque to the host."""


class Memory(Contract):
    id: str
    path: str
    content: str
    revision: Revision


class AgentThread(Contract):
    """A subagent thread inside a session (multiagent)."""

    id: str
    agent: ResourceRef | None = None
    parent_thread_id: str | None = None
    status: Literal["running", "idle", "terminated"]
