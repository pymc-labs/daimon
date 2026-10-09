"""Composable Anthropic Managed Agents factory using an existing SDK client."""

from typing import cast
from uuid import uuid4

from anthropic import AsyncAnthropic

from mux.contracts.admission import Admission, admit
from mux.contracts.config import ConfigRevision
from mux.contracts.extensions import ExtensionRef
from mux.contracts.ports import (
    Artifacts,
    Events,
    Models,
    SessionResources,
    Sessions,
    Skills,
    SkillVersions,
    Usage,
)
from mux.contracts.ports import PlatformExport as CorePlatformExport
from mux.contracts.ports import Vaults as CoreVaults
from mux.contracts.profile import Profile
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.resources._secrets import SecretResolver
from mux.drivers.anthropic.resources.agents import AnthropicAgents
from mux.drivers.anthropic.resources.artifacts import AnthropicArtifacts
from mux.drivers.anthropic.resources.artifacts import Artifacts as NativeArtifacts
from mux.drivers.anthropic.resources.environments import AnthropicEnvironments
from mux.drivers.anthropic.resources.platform_export import AnthropicPlatformExport, PlatformExport
from mux.drivers.anthropic.resources.sessions_admin import AnthropicSessionAdmin
from mux.drivers.anthropic.resources.sessions_admin import (
    SessionResources as NativeSessionResources,
)
from mux.drivers.anthropic.resources.skills import AnthropicSkills, AnthropicSkillVersions
from mux.drivers.anthropic.resources.vaults import AnthropicVaults, Vaults
from mux.drivers.anthropic.resources.walk import AnthropicResourceWalk, ResourceWalk
from mux.drivers.anthropic.turn import AnthropicEvents
from mux.errors import ExtensionVersionError, UnsupportedCapability
from mux.profiles.anthropic import MANAGED_AGENTS


def _required[T](port: T | None, name: str) -> T:
    if port is None:
        raise UnsupportedCapability((name,), MANAGED_AGENTS.profile_id)
    return port


class AnthropicManagedAgents:
    """Assemble one implementation per port; turn/session lanes can supply theirs.

    The host owns SDK client construction, settings, retries and lifetime. No
    provider call runs during construction and no raw client escapes a port.
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        *,
        account_scope_id: str | None = None,
        authorization: ResourceAuthorization | None = None,
        secrets: SecretResolver | None = None,
        sessions: Sessions | None = None,
        events: Events | None = None,
        artifacts: Artifacts | None = None,
        skills: Skills | None = None,
        models: Models | None = None,
        usage: Usage | None = None,
    ) -> None:
        self.account_scope_id = account_scope_id or str(uuid4())
        self.agents = AnthropicAgents(client, self.account_scope_id, authorization)
        self.environments = AnthropicEnvironments(client, self.account_scope_id, authorization)
        self._sessions = sessions
        self._events = events or AnthropicEvents(client, self.account_scope_id, authorization)
        native_artifacts = AnthropicArtifacts(client, self.account_scope_id, authorization, secrets)
        self._artifacts = artifacts or native_artifacts
        native_skills = AnthropicSkills(client, authorization)
        native_versions = AnthropicSkillVersions(client, authorization)
        self._skills = skills or native_skills
        self._models = models
        self._usage = usage
        native_vaults = AnthropicVaults(client, self.account_scope_id, secrets, authorization)
        self.session_admin = AnthropicSessionAdmin(
            client, self.account_scope_id, secrets, authorization
        )
        native_export = AnthropicPlatformExport(client)
        self._extensions: dict[tuple[type[object], str, int], object] = {
            (NativeArtifacts, "anthropic.artifacts", 1): native_artifacts,
            (Vaults, "anthropic.vaults", 1): native_vaults,
            (CoreVaults, "anthropic.vaults", 1): native_vaults,
            (SessionResources, "anthropic.session_resources", 1): self.session_admin,
            (NativeSessionResources, "anthropic.session_resources", 1): self.session_admin,
            (ResourceWalk, "anthropic.resource_walk", 1): AnthropicResourceWalk(
                self.agents, self.environments, native_skills, native_versions
            ),
            (PlatformExport, "anthropic.platform_export", 1): native_export,
            (CorePlatformExport, "anthropic.platform_export", 1): native_export,
            (SkillVersions, "anthropic.skills_versions", 1): native_versions,
        }

    @property
    def sessions(self) -> Sessions:
        return _required(self._sessions, "sessions")

    @property
    def events(self) -> Events:
        return _required(self._events, "events")

    @property
    def artifacts(self) -> Artifacts:
        return _required(self._artifacts, "artifacts")

    @property
    def skills(self) -> Skills:
        return _required(self._skills, "skills")

    @property
    def models(self) -> Models:
        return _required(self._models, "models")

    @property
    def usage(self) -> Usage:
        return _required(self._usage, "usage")

    def capabilities(self) -> Profile:
        addresses = {(namespace, version) for _, namespace, version in self._extensions}
        addresses.update(
            (namespace, 1)
            for namespace in (
                "anthropic.model_config",
                "anthropic.environment_config",
                "anthropic.agent_create_nulls",
            )
        )
        extra = tuple(
            ExtensionRef(namespace=namespace, version=version)
            for namespace, version in sorted(addresses)
            if not any(
                e.namespace == namespace and e.version == version for e in MANAGED_AGENTS.extensions
            )
        )
        return MANAGED_AGENTS.model_copy(update={"extensions": MANAGED_AGENTS.extensions + extra})

    def admit(self, config: ConfigRevision) -> Admission:
        return admit(config, self.capabilities())

    def extension[T](self, port: type[T], *, namespace: str, version: int) -> T:
        offered = tuple(
            e.version for e in self.capabilities().extensions if e.namespace == namespace
        )
        if not offered:
            raise UnsupportedCapability((namespace,), MANAGED_AGENTS.profile_id)
        if version not in offered:
            raise ExtensionVersionError(namespace, version, offered)
        implementation = self._extensions.get((port, namespace, version))
        if implementation is None:
            raise UnsupportedCapability((namespace,), MANAGED_AGENTS.profile_id)
        return cast(T, implementation)
