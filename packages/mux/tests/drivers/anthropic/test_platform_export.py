"""Every native export walk makes the same HTTP requests as the previous SDK walk."""

from collections.abc import AsyncIterator

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.testing.ma import MARouter
from daimon.testing.ma_transport import ScriptedTransport
from mux.contracts.ids import Scope
from mux.drivers.anthropic.resources.platform_export import AnthropicPlatformExport
from mux.errors import ProviderError

SCOPE = Scope(
    tenant_id="platform",
    account_id="operator",
    principal_id="operator",
    authorization_id="platform-export",
)


def client_and_requests():
    requests = []

    def respond(request):
        requests.append(
            (
                request.method,
                request.url.path,
                str(request.url.query),
                request.headers.get("anthropic-beta"),
                request.content,
            )
        )
        row = {
            "id": "resource",
            "version": 1 if "/agents" in request.url.path else "v1",
            "type": "memory",
            "path": "/facts",
            "content": "facts",
        }
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"zip content")
        if request.url.path in {
            "/v1/agents",
            "/v1/environments",
            "/v1/skills/s/versions",
            "/v1/memory_stores",
            "/v1/memory_stores/s/memories",
        }:
            cursor = request.url.params.get("page")
            return httpx.Response(
                200,
                json={
                    "data": [dict(row, id="second" if cursor else "first")],
                    "next_page": None if cursor else "second",
                },
            )
        return httpx.Response(200, json=row)

    router = MARouter()
    router.add("GET", r"/v1/.*", lambda request, _match: respond(request))
    transport = ScriptedTransport(router=router)
    return transport.client(), transport.requests


async def collect(iterator: AsyncIterator):
    return [item.model_dump(mode="json") async for item in iterator]


@pytest.mark.parametrize(
    "resource", ["agents", "environments", "skill_versions", "memory_stores", "memories"]
)
async def test_paginated_export_requests_and_native_json_match(resource):
    before, old_requests = client_and_requests()
    after, new_requests = client_and_requests()
    async with before, after:
        port = AnthropicPlatformExport(after)
        if resource == "agents":
            expected = await collect(before.beta.agents.list())
            actual = [v async for v in port.agents(SCOPE)]
        elif resource == "environments":
            expected = await collect(before.beta.environments.list())
            actual = [v async for v in port.environments(SCOPE)]
        elif resource == "skill_versions":
            expected = await collect(before.beta.skills.versions.list("s"))
            actual = [v async for v in port.skill_versions(SCOPE, "s")]
        elif resource == "memory_stores":
            expected = await collect(before.beta.memory_stores.list())
            actual = [v async for v in port.memory_stores(SCOPE)]
        else:
            expected = await collect(before.beta.memory_stores.memories.list("s", path_prefix="/"))
            actual = [v async for v in port.memories(SCOPE, "s")]
    assert actual == expected
    assert new_requests == old_requests
    assert len(new_requests) == 2


@pytest.mark.parametrize("resource", ["agent", "environment", "memory", "download"])
async def test_single_export_request_matches(resource):
    before, old_requests = client_and_requests()
    after, new_requests = client_and_requests()
    async with before, after:
        port = AnthropicPlatformExport(after)
        if resource == "agent":
            expected = (await before.beta.agents.retrieve("s")).model_dump(mode="json")
            actual = await port.agent(SCOPE, "s")
        elif resource == "environment":
            expected = (await before.beta.environments.retrieve("s")).model_dump(mode="json")
            actual = await port.environment(SCOPE, "s")
        elif resource == "memory":
            expected = (
                await before.beta.memory_stores.memories.retrieve(
                    "m", memory_store_id="s", view="full"
                )
            ).model_dump(mode="json")
            actual = await port.memory(SCOPE, "s", "m")
        else:
            response = await before.beta.skills.versions.download("v1", skill_id="s")
            try:
                expected = await response.read()
            finally:
                await response.close()
            actual = await port.download_skill_version(SCOPE, "s", "v1")
    assert actual == expected
    assert new_requests == old_requests
    assert len(new_requests) == 1


async def test_export_failure_does_not_leak_sdk_exception():
    def fail(request):
        return httpx.Response(
            403, json={"error": {"type": "permission_error", "message": "denied"}}
        )

    async with AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail)),
    ) as client:
        with pytest.raises(ProviderError) as caught:
            await AnthropicPlatformExport(client).agent(SCOPE, "s")
    assert caught.value.category == "permission"


async def test_native_export_requires_operator_scope_before_io():
    from mux.errors import ScopeViolation

    client, requests = client_and_requests()
    async with client:
        with pytest.raises(ScopeViolation):
            await AnthropicPlatformExport(client).agent(
                SCOPE.model_copy(update={"authorization_id": "ordinary-resource"}), "s"
            )
    assert requests == []


async def test_skill_summary_pages_preserve_missing_fields_and_limit():
    from mux.contracts.ids import PageRequest

    def summary(request):
        return httpx.Response(
            200,
            json={
                "data": [{"id": "skill1", "source": "custom", "type": "skill"}],
                "next_page": None if request.url.params.get("page") else "second",
            },
        )

    def transport():
        router = MARouter()
        router.add("GET", r"/v1/skills", lambda request, _match: summary(request))
        return ScriptedTransport(router=router)

    old, new = transport(), transport()
    async with old.client() as before, new.client() as after:
        expected = await collect(before.beta.skills.list(limit=1000))
        actual = []
        cursor = None
        while True:
            page = await AnthropicPlatformExport(after).skills_page(
                SCOPE, page=PageRequest(cursor=cursor, limit=1000)
            )
            actual.extend(page.data)
            if not page.has_more:
                break
            cursor = page.next_cursor
    assert actual == expected
    assert new.requests == old.requests
    old.assert_consumed()
    new.assert_consumed()
