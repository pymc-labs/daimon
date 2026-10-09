"""Build the default backend around the host's already-configured SDK client.

This introduces no client, setting, credential lookup or provider request.
Adapters retain ownership of client configuration and lifetime.
"""

from anthropic import AsyncAnthropic
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.resources._secrets import SecretResolver


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
