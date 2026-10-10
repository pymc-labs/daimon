"""Build the default backend around the host's already-configured SDK client.

This introduces no client, setting, credential lookup or provider request.
Adapters retain ownership of client configuration and lifetime.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from anthropic import AsyncAnthropic
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.resources._secrets import SecretResolver
from mux.drivers.anthropic.turn import AnthropicEvents
from mux.errors import ScopeViolation, UnsupportedCapability
from mux.profiles import get_profile

if TYPE_CHECKING:
    from daimon.core.turn.deps import TurnDeps


@dataclass(frozen=True)
class TurnRuntime:
    """Opaque provider-owned dependencies; host factories narrow their own types.

    The factory receives the admitted configuration and authorized scope.
    Transport implementations and journal/usage store protocols belong to
    the provider driver; this seam does not construct SDK clients.
    """

    transport_factory: Callable[[ConfigRevision, Scope], object]
    journal: object
    usage_store: object


@dataclass(frozen=True)
class TurnBackendRequest:
    """An admitted turn; provider factories own their injected runtime dependencies."""

    profile: str
    client: AsyncAnthropic
    scope: Scope
    session_id: str
    read_timeout_s: float = 120.0
    deps: TurnDeps | None = None
    config: ConfigRevision | None = None
    session: ResourceRef | None = None
    runtime: TurnRuntime | None = None

    @property
    def model(self) -> str | None:
        return self.config.model if self.config is not None else None


@dataclass(frozen=True)
class TurnBackend:
    backend: ManagedAgents
    session: ResourceRef


BackendFactory = Callable[[TurnBackendRequest], TurnBackend]
_TURN_BACKENDS: dict[str, BackendFactory] = {}


def register_turn_backend(profile: str, factory: BackendFactory) -> None:
    """Register a host composition at import/startup, without provider discovery."""
    if profile in _TURN_BACKENDS:
        raise ValueError(f"turn backend already registered: {profile}")
    _TURN_BACKENDS[profile] = factory


def _anthropic_turn_backend(request: TurnBackendRequest) -> TurnBackend:
    workspace = request.session.account_scope_id if request.session is not None else str(uuid4())
    authorization = ResourceAuthorization(
        request.scope, frozenset({("session", request.session_id)})
    )
    backend = AnthropicManagedAgents(
        request.client,
        account_scope_id=workspace,
        authorization=authorization,
        events=AnthropicEvents(
            request.client, workspace, authorization, stream_read_timeout_s=request.read_timeout_s
        ),
    )
    return TurnBackend(
        backend=backend,
        session=resource_ref(backend, "session", request.session_id, scope=request.scope),
    )


register_turn_backend("anthropic.managed_agents", _anthropic_turn_backend)


def turn_backend(request: TurnBackendRequest) -> TurnBackend:
    """Compose exactly the admitted profile, or fail before any provider I/O."""
    if request.config is not None and (
        request.config.profile != request.profile
        or request.config.channel.tenant_id != request.scope.tenant_id
    ):
        raise ScopeViolation(request.session_id, "backend request differs from admitted config")
    if request.profile != "anthropic.managed_agents" and request.session is None:
        raise ScopeViolation(request.session_id, "provider turn has no native prepared binding")
    if request.session is not None and (
        request.session.id != request.session_id
        or request.session.kind != "session"
        or request.session.tenant_id != request.scope.tenant_id
        or request.session.account_id != request.scope.account_id
        or request.session.provider != get_profile(request.profile).provider
    ):
        raise ScopeViolation(request.session_id, "backend request has a foreign native session")
    factory = _TURN_BACKENDS.get(request.profile)
    if factory is None:
        raise UnsupportedCapability(("host_turn_backend",), request.profile)
    bound = factory(request)
    profile = bound.backend.capabilities()
    session = bound.session
    if profile.profile_id != request.profile or (
        session.id != request.session_id
        or session.kind != "session"
        or session.provider != profile.provider
        or session.tenant_id != request.scope.tenant_id
        or session.account_id != request.scope.account_id
    ):
        raise ScopeViolation(request.session_id, "factory returned a foreign profile or session")
    if request.session is not None and session != request.session:
        raise ScopeViolation(request.session_id, "factory replaced the authorized native session")
    return bound


def managed_agents(
    client: AsyncAnthropic,
    *,
    scope: Scope | None = None,
    resources: frozenset[tuple[str, str]] = frozenset(),
    secrets: SecretResolver | None = None,
) -> AnthropicManagedAgents:
    return AnthropicManagedAgents(
        client,
        authorization=ResourceAuthorization(scope, resources) if scope is not None else None,
        secrets=secrets,
    )


def resource_ref(
    backend: AnthropicManagedAgents, kind: str, native_id: str, *, scope: Scope
) -> ResourceRef:
    return ResourceRef(
        id=native_id,
        kind=kind,
        provider="anthropic",
        account_scope_id=backend.account_scope_id,
        tenant_id=scope.tenant_id,
        account_id=scope.account_id,
    )


def resource_scope(
    *,
    tenant_id: str,
    account_id: str = "service",
    authorization_id: str = "host-resource",
) -> Scope:
    """Build a tenant scope from the host's existing authorization decision."""
    return Scope(
        tenant_id=tenant_id,
        account_id=account_id,
        principal_id="daimon",
        authorization_id=authorization_id,
    )


def platform_scope(reason: str, *, authorization_id: str = "platform-resource") -> Scope:
    return Scope.platform(reason=reason, authorization_id=authorization_id)
