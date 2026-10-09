"""Offline request fidelity: direct SDK calls versus neutral resource ports."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from daimon.core.mux_backend import platform_scope
from daimon.core.mux_compat import (
    archive_agent,
    archive_environment,
    create_agent,
    create_environment,
    list_agents,
    list_environments,
    retrieve_agent,
    update_agent,
    update_environment,
)
from daimon.testing.ma import MARouter
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_transport import ScriptedTransport


class ResourceTransport:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            (
                request.method,
                request.url.path,
                list(request.url.params.multi_items()),
                request.headers.get("anthropic-beta"),
                body,
            )
        )
        path = request.url.path
        if "/agents" in path:
            record = ma_agent(
                id="agent1",
                name="example",
                metadata={"daimon_tenant": "tenant"},
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ).model_dump(mode="json")
        else:
            record = ma_environment(
                id="env1", name="example", metadata={"daimon_tenant": "tenant"}
            ).model_dump(mode="json")
        if path.endswith("/archive"):
            return httpx.Response(200, json=record)
        if request.method == "GET" and path in {"/v1/agents", "/v1/environments"}:
            cursor = request.url.params.get("page")
            return httpx.Response(
                200, json={"data": [record], "next_page": None if cursor else "second"}
            )
        return httpx.Response(200, json=record)


def fake_client(transport):
    router = MARouter()
    for method in ("GET", "POST", "DELETE", "PATCH"):
        router.add(method, r"/v1/.*", lambda request, _match: transport(request))
    transport.scripted = ScriptedTransport(router=router)
    return transport.scripted.client()


def assert_transport_equal(before, after):
    before.scripted.assert_consumed()
    after.scripted.assert_consumed()

    def records(transport):
        result = []
        for index, request in enumerate(transport.scripted.requests):
            headers = dict(request.protocol_headers)
            multipart = headers.get("content-type", "").startswith("multipart/")
            if multipart:
                headers["content-type"] = "multipart/form-data"
            result.append(
                (
                    request.method,
                    request.path,
                    request.query,
                    headers,
                    transport.requests[index][-1] if multipart else request.json(),
                )
            )
        return result

    assert records(after) == records(before)
    assert after.requests == before.requests


@pytest.mark.parametrize("resource", ["agent", "environment"])
@pytest.mark.parametrize("include_archived", [False, True])
async def test_full_list_walk_makes_identical_requests(resource, include_archived):
    old = ResourceTransport()
    new = ResourceTransport()
    async with fake_client(old) as before, fake_client(new) as after:
        if resource == "agent":
            expected = [
                a.model_dump(mode="json")
                async for a in before.beta.agents.list(include_archived=include_archived)
            ]
            actual = [
                a.model_dump(mode="json")
                async for a in list_agents(
                    after,
                    include_archived=include_archived,
                    scope=platform_scope("test request fidelity"),
                )
            ]
        else:
            expected = [
                e.model_dump(mode="json")
                async for e in before.beta.environments.list(include_archived=include_archived)
            ]
            actual = [
                e.model_dump(mode="json")
                async for e in list_environments(
                    after,
                    include_archived=include_archived,
                    scope=platform_scope("test request fidelity"),
                )
            ]
    assert_transport_equal(old, new)
    assert len(new.requests) == 2
    assert actual == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "example", "model": "claude-sonnet-4-6"},
        {
            "name": "example",
            "model": "claude-sonnet-4-6",
            "description": "",
            "system": "",
            "tools": [],
            "mcp_servers": [],
            "skills": [],
            "metadata": {},
        },
        {
            "name": "example",
            "model": {"id": "claude-sonnet-4-6"},
            "tools": [
                {"type": "agent_toolset_20260401", "configs": [{"name": "bash", "enabled": False}]},
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "server",
                    "default_config": {"permission_policy": {"type": "always_ask"}},
                },
            ],
            "mcp_servers": [{"type": "url", "name": "server", "url": "https://example.test/mcp"}],
            "skills": [
                {"type": "anthropic", "skill_id": "xlsx"},
                {"type": "custom", "skill_id": "skill1", "version": "v1"},
            ],
            "multiagent": {"type": "coordinator", "agents": [{"type": "self"}]},
            "metadata": {"daimon_tenant": "tenant"},
        },
    ],
)
async def test_agent_create_has_no_extra_or_missing_fields(payload):
    old = ResourceTransport()
    new = ResourceTransport()
    async with fake_client(old) as before, fake_client(new) as after:
        expected = await before.beta.agents.create(**payload)
        actual = await create_agent(after, payload, scope=platform_scope("test request fidelity"))
    assert_transport_equal(old, new)
    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "example"},
        {
            "name": "example",
            "description": "",
            "scope": "account",
            "metadata": {"daimon_tenant": "tenant"},
        },
        {
            "name": "example",
            "config": {
                "type": "cloud",
                "packages": {
                    "apt": [],
                    "pip": ["pandas"],
                    "npm": [],
                    "cargo": [],
                    "gem": [],
                    "go": [],
                },
            },
        },
        {
            "name": "example",
            "config": {
                "type": "cloud",
                "networking": {"type": "limited", "allowed_hosts": [], "allow_mcp_servers": False},
            },
        },
    ],
)
async def test_environment_create_preserves_configuration(payload):
    old = ResourceTransport()
    new = ResourceTransport()
    async with fake_client(old) as before, fake_client(new) as after:
        expected = await before.beta.environments.create(**payload)
        actual = await create_environment(
            after, payload, scope=platform_scope("test request fidelity")
        )
    assert_transport_equal(old, new)
    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "example", "skills": []},
        {
            "description": None,
            "system": "",
            "tools": [],
            "mcp_servers": None,
            "metadata": {"removed": None},
        },
    ],
)
async def test_agent_update_keeps_version_and_null_clear_semantics(payload):
    old = ResourceTransport()
    new = ResourceTransport()
    async with fake_client(old) as before, fake_client(new) as after:
        await before.beta.agents.update("agent1", version=2, **payload)
        await update_agent(
            after,
            "agent1",
            version=2,
            payload=payload,
            scope=platform_scope("test request fidelity"),
        )
    assert_transport_equal(old, new)


@pytest.mark.parametrize(
    "resource", ["retrieve_agent", "archive_agent", "archive_environment", "update_environment"]
)
async def test_remaining_defaults_requests_match(resource):
    old = ResourceTransport()
    new = ResourceTransport()
    async with fake_client(old) as before, fake_client(new) as after:
        if resource == "retrieve_agent":
            await before.beta.agents.retrieve("agent1")
            await retrieve_agent(after, "agent1", scope=platform_scope("test request fidelity"))
        elif resource == "archive_agent":
            await before.beta.agents.archive("agent1")
            await archive_agent(after, "agent1", scope=platform_scope("test request fidelity"))
        elif resource == "archive_environment":
            await before.beta.environments.archive("env1")
            await archive_environment(after, "env1", scope=platform_scope("test request fidelity"))
        else:
            payload = {
                "description": "changed",
                "metadata": {"removed": None},
                "config": {"type": "cloud", "packages": {"pip": []}},
            }
            await before.beta.environments.update("env1", **payload)
            await update_environment(
                after, "env1", payload, scope=platform_scope("test request fidelity")
            )
    assert_transport_equal(old, new)


class SkillTransport:
    def __init__(self, *, fail_delete_version=False, terminal_full=False):
        self.requests = []
        self.fail_delete_version = fail_delete_version
        self.terminal_full = terminal_full

    def __call__(self, request):
        from email.parser import BytesParser
        from email.policy import default

        body = None
        if request.headers.get("content-type", "").startswith("multipart/"):
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: "
                + request.headers["content-type"].encode()
                + b"\r\n\r\n"
                + request.content
            )
            body = [
                (
                    part.get_param("name", header="content-disposition"),
                    part.get_filename(),
                    part.get_content_type(),
                    part.get_payload(decode=True),
                )
                for part in message.iter_parts()
            ]
        self.requests.append(
            (
                request.method,
                request.url.path,
                list(request.url.params.multi_items()),
                request.headers.get("anthropic-beta"),
                body,
            )
        )
        version = {
            "id": "sv1",
            "skill_id": "skill1",
            "version": "v1",
            "name": "example",
            "description": "desc",
            "directory": "example",
            "type": "skill_version",
            "created_at": "2026-01-01T00:00:00Z",
        }
        skill = {
            "id": "skill1",
            "display_title": "example",
            "source": "custom",
            "latest_version": "v1",
            "type": "skill",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        if request.method == "DELETE":
            if self.fail_delete_version and "/versions/" in request.url.path:
                return httpx.Response(
                    404, json={"error": {"type": "not_found_error", "message": "already gone"}}
                )
            return httpx.Response(200, json={"id": "skill1", "type": "skill", "deleted": True})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"ZIP")
        if request.method == "GET":
            row = version if "/versions" in request.url.path else skill
            cursor = request.url.params.get("page")
            data = [row] * (int(request.url.params["limit"]) if self.terminal_full else 1)
            return httpx.Response(
                200,
                json={
                    "data": data,
                    "next_page": None if cursor or self.terminal_full else "second",
                },
            )
        return httpx.Response(200, json=version if "/versions" in request.url.path else skill)


@pytest.mark.parametrize("operation", ["create", "publish", "download", "versions", "delete"])
async def test_skill_multipart_version_walk_and_cleanup_match(operation):
    import io

    from anthropic import APIStatusError
    from daimon.core.ma import delete_skill_and_versions
    from daimon.core.mux_compat import (
        create_skill,
        download_skill_version,
        list_skill_versions,
        publish_skill_version,
    )

    old = SkillTransport(fail_delete_version=operation == "delete")
    new = SkillTransport(fail_delete_version=operation == "delete")
    async with fake_client(old) as before, fake_client(new) as after:
        if operation == "create":
            expected = await before.beta.skills.create(
                display_title="example",
                files=[("SKILL.zip", io.BytesIO(b"ZIP"), "application/zip")],
            )
            actual = await create_skill(
                after,
                display_title="example",
                data=b"ZIP",
                scope=platform_scope("test request fidelity"),
            )
            assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
        elif operation == "publish":
            expected = await before.beta.skills.versions.create(
                "skill1", files=[("SKILL.zip", io.BytesIO(b"ZIP"), "application/zip")]
            )
            actual = await publish_skill_version(
                after, "skill1", data=b"ZIP", scope=platform_scope("test request fidelity")
            )
            assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
        elif operation == "download":
            response = await before.beta.skills.versions.download("v1", skill_id="skill1")
            try:
                expected = await response.read()
            finally:
                await response.close()
            assert (
                await download_skill_version(
                    after, "skill1", "v1", scope=platform_scope("test request fidelity")
                )
                == expected
            )
        elif operation == "versions":
            expected = [
                v.model_dump(mode="json")
                async for v in before.beta.skills.versions.list("skill1", limit=100)
            ]
            actual = [
                v.model_dump(mode="json")
                async for v in list_skill_versions(
                    after, "skill1", limit=100, scope=platform_scope("test request fidelity")
                )
            ]
            assert actual == expected
        else:
            async for version in before.beta.skills.versions.list("skill1", limit=100):
                try:
                    await before.beta.skills.versions.delete(version.version, skill_id="skill1")
                except APIStatusError as error:
                    if error.status_code != 404:
                        raise
            await before.beta.skills.delete("skill1")
            await delete_skill_and_versions(after, "skill1")
    assert_transport_equal(old, new)


@pytest.mark.parametrize("terminal_full", [False, True])
async def test_skills_listing_keeps_pagination_and_truncation_signal(terminal_full):
    from daimon.core.mux_compat import collect_skills

    old = SkillTransport(terminal_full=terminal_full)
    new = SkillTransport(terminal_full=terminal_full)
    async with fake_client(old) as before, fake_client(new) as after:
        expected = []
        truncated = False
        page = await before.beta.skills.list(limit=1000)
        async for current in page.iter_pages():
            expected.extend(s.model_dump(mode="json") for s in current.data)
            truncated |= len(current.data) >= 1000 and not current.next_page
        rows, actual_truncated = await collect_skills(
            after, limit=1000, scope=platform_scope("test request fidelity")
        )
    assert [s.model_dump(mode="json") for s in rows] == expected
    assert actual_truncated == truncated
    assert_transport_equal(old, new)


@pytest.mark.parametrize("resource", ["agent", "environment"])
@pytest.mark.parametrize("payload", [{}, {"data": None, "next_page": None}])
async def test_missing_page_data_keeps_sdks_empty_iterator_behavior(resource, payload):
    from collections import deque

    from daimon.testing.ma_transport import ScriptedReply

    path = "/v1/agents" if resource == "agent" else "/v1/environments"
    old = ScriptedTransport(deque([ScriptedReply("GET", path, httpx.Response(200, json=payload))]))
    new = ScriptedTransport(deque([ScriptedReply("GET", path, httpx.Response(200, json=payload))]))
    async with old.client() as before, new.client() as after:
        if resource == "agent":
            expected = [row async for row in before.beta.agents.list(include_archived=False)]
            actual = [
                row
                async for row in list_agents(
                    after, include_archived=False, scope=platform_scope("test request fidelity")
                )
            ]
        else:
            expected = [row async for row in before.beta.environments.list(include_archived=False)]
            actual = [
                row
                async for row in list_environments(
                    after, include_archived=False, scope=platform_scope("test request fidelity")
                )
            ]
    assert actual == expected == []
    assert new.requests == old.requests
    old.assert_consumed()
    new.assert_consumed()


@pytest.mark.parametrize("path", ["skills", "versions", "export"])
@pytest.mark.parametrize("stop_case", ["empty_with_cursor", "empty_cursor"])
async def test_skill_walks_keep_sdk_terminal_page_stop_rule(path, stop_case):
    from daimon.core.mux_compat import collect_skills, list_skill_versions
    from mux.contracts.ids import Scope
    from mux.drivers.anthropic.resources.platform_export import AnthropicPlatformExport

    old, new = SkillTransport(), SkillTransport()
    scope = Scope.platform(reason="test paginator stop", authorization_id="platform-export")

    def terminal(transport):
        def respond(request):
            reply = transport(request)
            payload = json.loads(reply.content)
            payload["next_page"] = "cursor" if stop_case == "empty_with_cursor" else ""
            if stop_case == "empty_with_cursor":
                payload["data"] = []
            return httpx.Response(200, json=payload)

        return respond

    def client(transport):
        router = MARouter()
        router.add("GET", r"/v1/.*", lambda request, _match: terminal(transport)(request))
        transport.scripted = ScriptedTransport(router=router)
        return transport.scripted.client()

    async with client(old) as before, client(new) as after:
        if path == "versions":
            expected = [
                v.model_dump(mode="json")
                async for v in before.beta.skills.versions.list("skill1", limit=100)
            ]
            actual = [
                v.model_dump(mode="json")
                async for v in list_skill_versions(after, "skill1", limit=100, scope=scope)
            ]
        else:
            initial = await before.beta.skills.list(limit=1000)
            expected = []
            async for page in initial.iter_pages():
                expected.extend(v.model_dump(mode="json") for v in page.data)
            if path == "skills":
                rows, truncated = await collect_skills(after, limit=1000, scope=scope)
                actual = [v.model_dump(mode="json") for v in rows]
                assert not truncated
            else:
                actual = []
                async for page in AnthropicPlatformExport(after).skill_pages(scope, limit=1000):
                    actual.extend(page.data)
    assert actual == expected
    assert_transport_equal(old, new)
    assert len(new.requests) == 1
