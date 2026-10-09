import httpx
import pytest
from anthropic import AsyncAnthropic
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources.platform_export import PlatformExport
from mux.errors import ExtensionVersionError, ScopeViolation, UnsupportedCapability

SCOPE = Scope(
    tenant_id="tenant",
    account_id="account",
    principal_id="principal",
    authorization_id="authorized",
)


def client():
    def no_requests(request):
        raise AssertionError("operation must be rejected before HTTP")

    return AsyncAnthropic(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(no_requests))
    )


async def test_factory_has_no_io_and_reuses_injected_client_lifetime():
    async with client() as sdk:
        backend = AnthropicManagedAgents(sdk, account_scope_id="workspace")
        assert backend.account_scope_id == "workspace"
        assert backend.capabilities().profile_id == "anthropic.managed_agents"
        assert (
            backend.extension(PlatformExport, namespace="anthropic.platform_export", version=1)
            is not None
        )
        assert not sdk.is_closed()


async def test_unknown_extension_and_wrong_version_are_distinct():
    async with client() as sdk:
        backend = AnthropicManagedAgents(sdk)
        with pytest.raises(UnsupportedCapability):
            backend.extension(PlatformExport, namespace="anthropic.unavailable", version=1)
        with pytest.raises(ExtensionVersionError):
            backend.extension(PlatformExport, namespace="anthropic.platform_export", version=2)


@pytest.mark.parametrize(
    "provider,workspace,kind",
    [
        ("openai", "workspace", "agent"),
        ("anthropic", "other", "agent"),
        ("anthropic", "workspace", "session"),
    ],
)
async def test_foreign_reference_is_rejected_before_io(provider, workspace, kind):
    async with client() as sdk:
        backend = AnthropicManagedAgents(sdk, account_scope_id="workspace")
        ref = ResourceRef(id="resource", kind=kind, provider=provider, account_scope_id=workspace)
        with pytest.raises(ScopeViolation):
            await backend.agents.retrieve(SCOPE, ref)


async def test_agent_hard_delete_never_substitutes_archive():
    async with client() as sdk:
        backend = AnthropicManagedAgents(sdk, account_scope_id="workspace")
        ref = ResourceRef(
            id="resource", kind="agent", provider="anthropic", account_scope_id="workspace"
        )
        with pytest.raises(UnsupportedCapability):
            await backend.agents.delete(
                Scope.platform(reason="test hard delete"), ref, key="delete"
            )
