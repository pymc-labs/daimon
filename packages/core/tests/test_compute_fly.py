"""Fly wire contracts use MockTransport and a local thin MCP server, never Fly."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.core.compute_fly import FlyComputeProvider
from daimon.core.compute_models import ComputeResource, ComputeSession
from daimon.core.compute_provider import ComputeProviderError, app_name
from daimon.core.config import ComputeSettings
from fastmcp import FastMCP
from pydantic import SecretStr


def config():
    return ComputeSettings(
        deployment_id="deployment-a",
        org_slug="org",
        api_token=SecretStr("fly-test-token"),
        session_auth_key=SecretStr("hmac-test-key"),
    )


def session():
    session_id = uuid.uuid4()
    return ComputeSession(
        id=session_id,
        tenant_id=uuid.uuid4(),
        owner_account_id=uuid.uuid4(),
        owner_external_user_id="user",
        deployment_id="deployment-a",
        platform="discord",
        parent_channel_id="parent",
        thread_id="thread",
        agent_id="agent",
        agent_name="a",
        request_key="request",
        app_name=app_name("deployment-a", session_id),
        profile="mmm",
        image="mmm@sha256:abc",
        region="iad",
        cpu_kind="shared",
        cpus=2,
        memory_mb=4096,
        ttl_hours=24,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        created_at=datetime.now(UTC),
    )


async def test_create_recovery_ip_reuse_control_credentials_and_confirmed_delete():
    owned = session()
    app_exists = False
    machines = []
    addresses = []
    requests = []

    async def handler(request):
        nonlocal app_exists, machines
        requests.append(request)
        assert request.headers["authorization"] == "Bearer fly-test-token"
        if request.method == "GET" and request.url.path.endswith("/machines"):
            return httpx.Response(200, json=machines)
        if request.method == "GET" and request.url.path.endswith("/ip_assignments"):
            return httpx.Response(200, json={"ips": addresses})
        if request.method == "GET":
            return httpx.Response(200 if app_exists else 404, json={"name": owned.app_name})
        if request.method == "DELETE":
            assert request.url.params["force"] == "true"
            app_exists, machines = False, []
            return httpx.Response(202)
        body = json.loads(request.content)
        if request.url.path == "/v1/apps":
            assert body == {
                "app_name": owned.app_name,
                "org_slug": "org",
                "network": owned.app_name,
            }
            app_exists = True
            return httpx.Response(201, json={})
        if request.url.path.endswith("/ip_assignments"):
            addresses.append(
                {
                    "ip": "1.2.3.4" if body["type"] == "shared_v4" else "::1",
                    "shared": body["type"] == "shared_v4",
                }
            )
            return httpx.Response(200, json={})
        assert body["config"]["env"] == {"MCP_AUTH_TOKEN": "machine-token"}
        assert "fly-test-token" not in request.content.decode()
        assert "hmac-test-key" not in request.content.decode()
        machines = [{"id": "machine", "state": "started", "config": body["config"]}]
        return httpx.Response(200, json=machines[0])

    provider = FlyComputeProvider(config(), transport=httpx.MockTransport(handler))
    resource = await provider.ensure(owned, "machine-token")
    assert resource.machine_id == "machine"
    assert await provider.ensure(owned, "machine-token") == resource
    assert len(addresses) == 2
    assert (
        len([r for r in requests if r.method == "POST" and r.url.path.endswith("/machines")]) == 1
    )
    # Resume a partial app that already owns both addresses.
    machines.clear()
    await provider.ensure(owned, "machine-token")
    assert len(addresses) == 2
    await provider.delete(resource)
    assert not app_exists


async def test_inventory_keeps_empty_apps_and_ignores_foreign_or_conflicting_tags():
    own, empty, conflict = session(), session(), session()
    foreign = app_name("deployment-b", uuid.uuid4())

    async def handler(request):
        if request.url.path == "/v1/apps":
            return httpx.Response(
                200,
                json={
                    "apps": [
                        {"name": name}
                        for name in (
                            own.app_name,
                            empty.app_name,
                            conflict.app_name,
                            foreign,
                            "production",
                        )
                    ]
                },
            )
        assert foreign not in str(request.url) and "production" not in str(request.url)
        if empty.app_name in str(request.url):
            return httpx.Response(200, json=[])
        target = own if own.app_name in str(request.url) else conflict
        return httpx.Response(
            200,
            json=[
                {
                    "id": "machine",
                    "config": {
                        "metadata": {
                            "daimon_session": str(target.id),
                            "daimon_deployment": "deployment-a"
                            if target == own
                            else "deployment-b",
                        }
                    },
                }
            ],
        )

    provider = FlyComputeProvider(config(), transport=httpx.MockTransport(handler))
    resources = await provider.inventory()
    assert {resource.session_id for resource in resources} == {own.id, empty.id}
    with pytest.raises(ComputeProviderError):
        await provider.delete(
            ComputeResource(
                session_id=conflict.id,
                app_name=conflict.app_name,
                deployment_id=conflict.deployment_id,
            )
        )


@pytest.mark.parametrize("json_response", [True, False])
@pytest.mark.parametrize("stateless", [True, False])
async def test_inline_thin_mcp_over_http(json_response, stateless):
    thin = FastMCP("thin-test")

    @thin.tool
    async def run_bash(command: str, timeout_s: int):
        assert timeout_s == 40
        return {"exit_code": 0, "stdout": command, "timed_out": False}

    # task=True on the deployed image means optional task support; a normal
    # tools/call without task augmentation uses this same inline wire path.
    app = thin.http_app(path="/mcp/", stateless_http=stateless, json_response=json_response)
    async with app.router.lifespan_context(app):
        provider = FlyComputeProvider(config(), transport=httpx.ASGITransport(app=app))
        result = await provider.call(
            session(), "per-session-token", "run_bash", {"command": "hello", "timeout_s": 40}
        )
    assert result == {"exit_code": 0, "stdout": "hello", "timed_out": False}


async def test_delete_is_not_confirmed_by_accepted_response():
    owned = session()

    async def handler(request):
        if request.url.path.endswith("/machines"):
            return httpx.Response(200, json=[])
        return httpx.Response(202 if request.method == "DELETE" else 200, json={})

    provider = FlyComputeProvider(config(), transport=httpx.MockTransport(handler))
    with pytest.raises(ComputeProviderError, match="pending"):
        await provider.delete(
            ComputeResource(
                app_name=owned.app_name, deployment_id=owned.deployment_id, session_id=owned.id
            )
        )
