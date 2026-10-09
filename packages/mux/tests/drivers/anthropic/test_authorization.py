"""Tenant checks happen before I/O; tagged-record checks add no requests."""

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.ids import PageRequest, ResourceRef, Revision, Scope, SkillRef
from mux.contracts.ports import SkillVersions
from mux.contracts.resources import AgentFilter, AgentPatch, EnvironmentFilter, EnvironmentPatch
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.resources.walk import ResourceWalk
from mux.errors import ScopeViolation

A = Scope(tenant_id="A", account_id="account-A", principal_id="host", authorization_id="auth-A")
B = Scope(tenant_id="B", account_id="account-B", principal_id="host", authorization_id="auth-B")


def ref(kind, *, tenant="A", account="account-A"):
    return ResourceRef(
        id="resource",
        kind=kind,
        provider="anthropic",
        account_scope_id="workspace",
        tenant_id=tenant,
        account_id=account,
    )


def sdk_client(requests, *, tenant="A", mixed=False):
    def respond(request):
        requests.append(request)
        kind = "agent" if "/agents" in request.url.path else "environment"
        factory = ma_agent if kind == "agent" else ma_environment
        row = factory(id="resource", metadata={"daimon_tenant": tenant}).model_dump(mode="json")
        if request.url.path in {"/v1/agents", "/v1/environments"}:
            other = factory(id="foreign", metadata={"daimon_tenant": "B"}).model_dump(mode="json")
            return httpx.Response(
                200, json={"data": [row, other] if mixed else [row], "next_page": None}
            )
        return httpx.Response(200, json=row)

    return AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )


def backend(
    client,
    scope=A,
    resources=frozenset({("agent", "resource"), ("environment", "resource"), ("skill", "skill")}),
):
    return AnthropicManagedAgents(
        client, account_scope_id="workspace", authorization=ResourceAuthorization(scope, resources)
    )


@pytest.mark.parametrize("kind", ["agent", "environment"])
@pytest.mark.parametrize("operation", ["retrieve", "update", "archive", "delete"])
@pytest.mark.parametrize("tenant,account", [("B", "account-A"), ("A", "account-B"), (None, None)])
async def test_foreign_or_unowned_ref_rejected_before_io(kind, operation, tenant, account):
    requests = []
    async with sdk_client(requests) as client:
        port = backend(client).agents if kind == "agent" else backend(client).environments
        resource = ref(kind, tenant=tenant, account=account)
        with pytest.raises(ScopeViolation):
            if operation == "retrieve":
                await port.retrieve(A, resource)
            elif operation == "update":
                patch = AgentPatch(name="new") if kind == "agent" else EnvironmentPatch(name="new")
                await port.update(A, resource, patch, expected=Revision(local=1), key="update")
            else:
                await getattr(port, operation)(A, resource, key=operation)
    assert requests == []


async def test_reviewers_same_workspace_cross_tenant_repro_is_rejected_before_io():
    requests = []
    async with sdk_client(requests) as client:
        with pytest.raises(ScopeViolation):
            await AnthropicManagedAgents(client, account_scope_id="workspace").agents.retrieve(
                B, ref("agent", tenant=None, account=None)
            )
    assert requests == []


@pytest.mark.parametrize(
    "operation", ["retrieve", "delete", "publish", "versions", "download", "delete_version"]
)
async def test_skill_operations_require_host_ownership_and_matching_scope(operation):
    from mux.contracts.resources import SkillUpload, SkillUploadFile

    requests = []
    async with sdk_client(requests) as client:
        driver = backend(client)
        versions = driver.extension(SkillVersions, namespace="anthropic.skills_versions", version=1)
        with pytest.raises(ScopeViolation):
            if operation == "retrieve":
                await driver.skills.retrieve(B, "skill")
            elif operation == "delete":
                await driver.skills.delete(B, "skill", key="delete")
            elif operation == "publish":
                await driver.skills.publish_version(
                    B,
                    "skill",
                    SkillUpload(files=(SkillUploadFile(path="SKILL.zip", content=b"ZIP"),)),
                    key="publish",
                )
            elif operation == "versions":
                await versions.versions(B, "skill", page=PageRequest())
            elif operation == "download":
                await anext(versions.download(B, SkillRef(id="skill", version="v1")))
            else:
                await versions.delete_version(B, SkillRef(id="skill", version="v1"), key="delete")
        with pytest.raises(ScopeViolation):
            await driver.skills.retrieve(A, "unowned")
    assert requests == []


@pytest.mark.parametrize("kind", ["agent", "environment"])
async def test_minted_refs_carry_authorized_tenant_and_account(kind):
    requests = []
    async with sdk_client(requests) as client:
        driver = backend(client)
        port = driver.agents if kind == "agent" else driver.environments
        record = await port.retrieve(A, ref(kind))
    assert record.ref.tenant_id == A.tenant_id
    assert record.ref.account_id == A.account_id
    assert len(requests) == 1


@pytest.mark.parametrize("kind", ["agent", "environment"])
async def test_native_foreign_tenant_tag_is_rejected_with_no_extra_request(kind):
    requests = []
    async with sdk_client(requests, tenant="B") as client:
        driver = backend(client)
        port = driver.agents if kind == "agent" else driver.environments
        with pytest.raises(ScopeViolation):
            await port.retrieve(A, ref(kind))
    assert len(requests) == 1


@pytest.mark.parametrize("kind", ["agent", "environment"])
@pytest.mark.parametrize("platform", [False, True])
async def test_list_and_legacy_walk_filter_only_nonplatform_scope(kind, platform):
    requests = []
    scope = Scope.platform(reason="test workspace sweep") if platform else A
    async with sdk_client(requests, mixed=True) as client:
        driver = backend(client)
        walk = driver.extension(ResourceWalk, namespace="anthropic.resource_walk", version=1)
        port = driver.agents if kind == "agent" else driver.environments
        filters = AgentFilter() if kind == "agent" else EnvironmentFilter()
        page = await port.list(scope, filters=filters, page=PageRequest())
        items = [item async for item in getattr(walk, kind + "s")(scope, filters=filters)]
    assert [item.ref.id for item in page.data] == (
        ["resource", "foreign"] if platform else ["resource"]
    )
    assert [item.ref.id for item in items] == (
        ["resource", "foreign"] if platform else ["resource"]
    )
    assert len(requests) == 2


async def test_platform_archive_preserves_unstamped_legacy_reference_request():
    requests = []
    async with sdk_client(requests) as client:
        await AnthropicManagedAgents(client, account_scope_id="workspace").agents.archive(
            Scope.platform(reason="test defaults sweep"),
            ref("agent", tenant=None, account=None),
            key="archive",
        )
    assert [(r.method, r.url.path) for r in requests] == [("POST", "/v1/agents/resource/archive")]


async def test_bound_authorization_rejects_foreign_create_list_and_walk_before_io():
    from mux.contracts.ids import ModelRef
    from mux.contracts.resources import AgentSpec, EnvironmentSpec, SkillUpload, SkillUploadFile

    requests = []
    async with sdk_client(requests) as client:
        driver = backend(client)
        walk = driver.extension(ResourceWalk, namespace="anthropic.resource_walk", version=1)
        operations = [
            driver.agents.create(
                B,
                AgentSpec(name="agent", model=ModelRef(provider="anthropic", id="model")),
                key="create",
            ),
            driver.environments.create(B, EnvironmentSpec(name="environment"), key="create"),
            driver.skills.create(
                B,
                SkillUpload(files=(SkillUploadFile(path="SKILL.zip", content=b"ZIP"),)),
                key="create",
            ),
            driver.agents.list(B, filters=AgentFilter(), page=PageRequest()),
            driver.environments.list(B, filters=EnvironmentFilter(), page=PageRequest()),
            driver.skills.list(B, page=PageRequest()),
            anext(walk.agents(B, filters=AgentFilter())),
            anext(walk.environments(B, filters=EnvironmentFilter())),
            anext(walk.skill_pages(B, limit=1000)),
            anext(walk.skill_versions(B, "skill", limit=100)),
        ]
        for operation in operations:
            with pytest.raises(ScopeViolation):
                await operation
    assert requests == []
