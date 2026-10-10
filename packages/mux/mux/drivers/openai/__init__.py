"""Unwired OpenAI Agents API driver; construction never discovers or calls a provider."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from mux.contracts.admission import Admission, admit
from mux.contracts.config import ConfigRevision
from mux.contracts.ports import Artifacts, Events, ManagedAgents, Models, Skills, Steering, Vaults
from mux.contracts.profile import Profile
from mux.drivers.openai._common import Authorization, Context
from mux.drivers.openai.agents import OpenAIAgents
from mux.drivers.openai.artifacts import OpenAIArtifacts
from mux.drivers.openai.environments import OpenAIEnvironments
from mux.drivers.openai.mcp_auth import MCPSecretResolver
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.sessions import BindingLookup, OpenAISessions
from mux.drivers.openai.skills import OpenAISkills
from mux.drivers.openai.transport import Transport
from mux.drivers.openai.turn import OpenAIEvents, RecoveryJournal
from mux.drivers.openai.usage import OpenAIUsage, UsageRevisions
from mux.drivers.openai.vaults import OpenAIVaults, SecretResolver
from mux.errors import ExtensionVersionError, UnsupportedCapability
from mux.profiles.openai import CONVERSATION_ONLY, PERSISTENT_WORKSPACE


class OpenAIDriver:
    """Explicit profile and host state. SDK handles remain inside the transport.

    Resource ports use the actual SDK by default; host implementations may be injected.
    There is no host registry/default/configuration change in this package.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        account_scope_id: str,
        journal: RecoveryJournal,
        usage_revisions: UsageRevisions,
        profile_id: str = "openai.persistent_workspace",
        authorization: Authorization | None = None,
        binding_lookup: BindingLookup | None = None,
        artifacts: Artifacts | None = None,
        skills: Skills | None = None,
        models: Models | None = None,
        vaults: Vaults | None = None,
        secrets: SecretResolver | None = None,
        events: Events | None = None,
        session_controls: SessionControls | None = None,
        mcp_secrets: MCPSecretResolver | None = None,
    ) -> None:
        profiles = {p.profile_id: p for p in (PERSISTENT_WORKSPACE, CONVERSATION_ONLY)}
        if profile_id not in profiles:
            raise ValueError("unknown OpenAI profile")
        self._profile = profiles[profile_id]
        context = Context(transport, account_scope_id, profile_id, authorization)
        self.agents = OpenAIAgents(context)
        self.environments = OpenAIEnvironments(context)
        self.sessions = OpenAISessions(
            context, binding_lookup, controls=session_controls, mcp_secrets=mcp_secrets
        )
        self.events: Events = (
            events if events is not None else OpenAIEvents(context, self.sessions, journal)
        )
        self.usage = OpenAIUsage(context, usage_revisions)
        self._artifacts = artifacts if artifacts is not None else OpenAIArtifacts(context)
        self._skills = skills if skills is not None else OpenAISkills(context)
        self._models = models
        vaults = vaults if vaults is not None else OpenAIVaults(context, secrets)
        self._extensions: dict[tuple[type[object], str, int], object] = {}
        if events is None:
            self._extensions[Steering, "openai.steer", 1] = self.events
        self._extensions[Vaults, "openai.vaults", 1] = vaults

    def _required[T](self, implementation: T | None, name: str) -> T:
        if implementation is None:
            raise UnsupportedCapability((name,), self._profile.profile_id)
        return implementation

    @property
    def artifacts(self) -> Artifacts:
        return self._required(self._artifacts, "artifacts")

    @property
    def skills(self) -> Skills:
        return self._required(self._skills, "skills")

    @property
    def models(self) -> Models:
        return self._required(self._models, "models")

    def capabilities(self) -> Profile:
        return self._profile

    def admit(self, config: ConfigRevision) -> Admission:
        return admit(config, self.capabilities())

    def extension[T](self, port: type[T], *, namespace: str, version: int) -> T:
        versions = tuple(e.version for e in self._profile.extensions if e.namespace == namespace)
        if not versions:
            raise UnsupportedCapability((namespace,), self._profile.profile_id)
        if version not in versions:
            raise ExtensionVersionError(namespace, version, versions)
        implementation = self._extensions.get((port, namespace, version))
        if implementation is None:
            raise UnsupportedCapability((namespace,), self._profile.profile_id)
        return cast(T, implementation)


if TYPE_CHECKING:
    _checked_factory: Callable[..., ManagedAgents] = OpenAIDriver
