"""Build the default backend around the host's already-configured SDK client.

This introduces no client, setting, credential lookup or provider request.
Adapters retain ownership of client configuration and lifetime.
"""

from anthropic import AsyncAnthropic
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic import AnthropicManagedAgents


def managed_agents(client: AsyncAnthropic) -> AnthropicManagedAgents:
    return AnthropicManagedAgents(client)


def resource_ref(backend: AnthropicManagedAgents, kind: str, native_id: str) -> ResourceRef:
    return ResourceRef(
        id=native_id, kind=kind, provider="anthropic", account_scope_id=backend.account_scope_id
    )


def resource_scope(
    *,
    tenant_id: str = "platform",
    account_id: str = "service",
    authorization_id: str = "legacy-resource",
) -> Scope:
    """Host-internal operations retain their existing authorization boundaries."""
    return Scope(
        tenant_id=tenant_id,
        account_id=account_id,
        principal_id="daimon",
        authorization_id=authorization_id,
    )
