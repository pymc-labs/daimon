"""Cloud Run wire contracts use MockTransport and a local thin MCP server, never Google."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.core import compute_cloud_run
from daimon.core.compute_cloud_run import (
    ApplicationDefaultTokens,
    CloudRunComputeProvider,
    deployment_label,
)
from daimon.core.compute_models import ComputeResource, ComputeSession
from daimon.core.compute_provider import ComputeProviderError, app_name
from daimon.core.config import ComputeProfile, ComputeSettings
from fastmcp import FastMCP
from google.auth import exceptions as google_exceptions
from pydantic import SecretStr

PARENT = "/v2/projects/test-project/locations/us-central1/services"
SERVICE_ACCOUNT = "session@test-project.iam.gserviceaccount.com"


class Tokens:
    """Stands in for application default credentials; records each audience asked for."""

    def __init__(self) -> None:
        self.audiences: list[str] = []

    async def access_token(self) -> str:
        return "google-access-token"

    async def id_token(self, audience: str) -> str:
        self.audiences.append(audience)
        return "google-identity-token"


def config(**kwargs):
    return ComputeSettings(
        provider="cloud_run",
        deployment_id="deployment-a",
        cloud_run_project="test-project",
        cloud_run_service_account=SERVICE_ACCOUNT,
        session_auth_key=SecretStr("hmac-test-key"),
        profiles={
            "mmm": ComputeProfile(image="mmm@sha256:abc", regions=["us-central1", "europe-west4"])
        },
        **kwargs,
    )


def session(deployment="deployment-a", region="us-central1"):
    session_id = uuid.uuid4()
    return ComputeSession(
        id=session_id,
        tenant_id=uuid.uuid4(),
        owner_account_id=uuid.uuid4(),
        owner_external_user_id="user",
        deployment_id=deployment,
        platform="discord",
        parent_channel_id="parent",
        thread_id="thread",
        agent_id="agent",
        agent_name="a",
        request_key="request",
        app_name=app_name(deployment, session_id),
        profile="mmm",
        image="mmm@sha256:abc",
        region=region,
        cpu_kind="shared",
        cpus=2,
        memory_mb=4096,
        ttl_hours=24,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        created_at=datetime.now(UTC),
    )


def resource_of(owned, **update):
    return ComputeResource(
        app_name=owned.app_name,
        deployment_id=owned.deployment_id,
        session_id=owned.id,
        region=owned.region,
    ).model_copy(update=update)


def service_json(owned, *, deployment=None, state="CONDITION_SUCCEEDED", **extra):
    return {
        "name": f"projects/test-project/locations/{owned.region}/services/{owned.app_name}",
        "uid": "uid-" + owned.id.hex,
        "labels": {
            "daimon_deployment": deployment_label(deployment or owned.deployment_id),
            "daimon_session": owned.id.hex,
        },
        "uri": f"https://{owned.app_name}-abc123-uc.a.run.app",
        "terminalCondition": {"type": "Ready", "state": state},
        **extra,
    }


def provider(handler, **kwargs):
    return CloudRunComputeProvider(
        config(), transport=httpx.MockTransport(handler), tokens=Tokens(), **kwargs
    )


async def test_create_is_private_single_instance_and_recovers_without_a_second_create():
    owned = session()
    stored: dict | None = None
    creates = []

    async def handler(request):
        nonlocal stored
        assert request.headers["authorization"] == "Bearer google-access-token"
        assert request.url.host == "run.googleapis.com"
        if request.method == "GET":
            assert request.url.path == f"{PARENT}/{owned.app_name}"
            return httpx.Response(200, json=stored) if stored else httpx.Response(404)
        assert request.method == "POST" and request.url.path == PARENT
        assert request.url.params["serviceId"] == owned.app_name
        creates.append(json.loads(request.content))
        if stored:
            return httpx.Response(409, json={"error": {"status": "ALREADY_EXISTS"}})
        stored = service_json(owned, state="CONDITION_RECONCILING", reconciling=True)
        return httpx.Response(200, json={"name": "operations/create", "done": False})

    cloud_run = provider(handler)
    created = await cloud_run.ensure(owned, "machine-token")
    assert created.machine_id == "uid-" + owned.id.hex
    assert created.machine_state == "starting"
    (body,) = creates
    container = body["template"]["containers"][0]
    assert body["labels"] == {
        "daimon_deployment": deployment_label("deployment-a"),
        "daimon_session": owned.id.hex,
    }
    assert body["ingress"] == "INGRESS_TRAFFIC_ALL" and body["invokerIamDisabled"] is False
    assert body["scaling"] == {"minInstanceCount": 1, "maxInstanceCount": 1}
    assert body["template"]["serviceAccount"] == SERVICE_ACCOUNT
    assert body["template"]["timeout"] == "3600s"
    assert container["image"] == "mmm@sha256:abc"
    assert container["ports"] == [{"containerPort": 8080}]
    assert container["env"] == [{"name": "MCP_AUTH_TOKEN", "value": "machine-token"}]
    assert container["resources"]["limits"] == {"cpu": "2", "memory": "4096Mi"}
    # CPU stays allocated between requests, so background commands keep running.
    assert container["resources"]["cpuIdle"] is False
    assert "hmac-test-key" not in json.dumps(body)
    assert "google-access-token" not in json.dumps(body)
    # A lost create response is recovered by the deterministic name.
    assert (await cloud_run.ensure(owned, "machine-token")).machine_id == created.machine_id
    assert len(creates) == 1


async def test_a_racing_create_is_recovered_by_reading_the_service():
    owned = session()
    reads = 0

    async def handler(request):
        nonlocal reads
        if request.method == "POST":
            return httpx.Response(409, json={})
        reads += 1
        return httpx.Response(404) if reads == 1 else httpx.Response(200, json=service_json(owned))

    resource = await provider(handler).ensure(owned, "machine-token")
    assert resource.machine_state == "started"


async def test_foreign_services_are_never_adopted_called_or_deleted():
    owned = session()
    seen = []

    async def handler(request):
        seen.append(request.method)
        return httpx.Response(200, json=service_json(owned, deployment="deployment-b"))

    cloud_run = provider(handler)
    for operation in (
        cloud_run.ensure(owned, "machine-token"),
        cloud_run.inspect(owned),
        cloud_run.delete(resource_of(owned)),
        cloud_run.call(owned, "machine-token", "run_bash", {}),
    ):
        with pytest.raises(ComputeProviderError, match="ownership"):
            await operation
    assert set(seen) == {"GET"}
    # Another deployment's session is refused before any request.
    seen.clear()
    with pytest.raises(ComputeProviderError, match="ownership"):
        await cloud_run.inspect(session(deployment="deployment-b"))
    assert not seen


@pytest.mark.parametrize(
    ("service", "expected"),
    [
        (None, "no_machines"),
        ({"terminalCondition": {"state": "CONDITION_SUCCEEDED"}}, "started"),
        ({"terminalCondition": {"state": "CONDITION_SUCCEEDED"}, "reconciling": True}, "starting"),
        ({"terminalCondition": {"state": "CONDITION_RECONCILING"}}, "starting"),
        ({"terminalCondition": {"state": "CONDITION_PENDING"}}, "starting"),
        ({"terminalCondition": None}, "starting"),
        ({"terminalCondition": {"state": "CONDITION_FAILED"}}, "failed"),
        ({"deleteTime": "2026-11-24T00:00:00Z"}, "destroying"),
    ],
)
async def test_status_maps_cloud_run_readiness_to_machine_states(service, expected):
    owned = session()

    async def handler(request):
        if service is None:
            return httpx.Response(404)
        body = {**service_json(owned), **service}
        return httpx.Response(200, json={k: v for k, v in body.items() if v is not None})

    assert (await provider(handler).inspect(owned)).machine_state == expected


async def test_delete_waits_for_google_and_is_confirmed_only_by_a_missing_service():
    owned = session()
    exists, finishes_on_wait = True, True
    requests = []

    async def handler(request):
        nonlocal exists
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=service_json(owned)) if exists else httpx.Response(404)
        if request.method == "DELETE":
            return httpx.Response(
                200,
                json={
                    "name": "projects/test-project/locations/us-central1/operations/op-1",
                    "done": False,
                },
            )
        assert request.url.path == (
            "/v2/projects/test-project/locations/us-central1/operations/op-1:wait"
        )
        assert json.loads(request.content) == {"timeout": "20s"}
        exists = not finishes_on_wait
        return httpx.Response(200, json={"done": finishes_on_wait})

    cloud_run = provider(handler)
    await cloud_run.delete(resource_of(owned))
    assert [method for method, _ in requests] == ["GET", "DELETE", "POST", "GET"]
    # Deleting what is already gone succeeds without a delete request.
    requests.clear()
    await cloud_run.delete(resource_of(owned))
    assert [method for method, _ in requests] == ["GET"]
    # An accepted delete that Google has not finished stays pending for the next sweep.
    exists, finishes_on_wait = True, False
    with pytest.raises(ComputeProviderError, match="pending"):
        await cloud_run.delete(resource_of(owned))


async def test_delete_without_a_region_searches_every_profile_region():
    owned = session(region="europe-west4")
    exists = True
    regions = []

    async def handler(request):
        nonlocal exists
        region = request.url.path.split("/")[5]
        regions.append((request.method, region))
        if region != "europe-west4" or not exists:
            return httpx.Response(404)
        if request.method == "DELETE":
            exists = False
            return httpx.Response(200, json={"done": True})
        return httpx.Response(200, json=service_json(owned))

    await provider(handler).delete(resource_of(owned, region=None))
    assert ("DELETE", "europe-west4") in regions
    assert {region for _, region in regions} == {"europe-west4", "us-central1"}


async def test_inventory_pages_every_region_and_ignores_foreign_or_conflicting_labels():
    own, second_page, far, conflict = session(), session(), session("deployment-a"), session()
    foreign = session(deployment="deployment-b")

    def named(name):
        return {"name": f"projects/test-project/locations/x/services/{name}", "labels": {}}

    async def handler(request):
        region = request.url.path.split("/")[5]
        assert request.url.path.endswith("/services") and request.method == "GET"
        if region == "europe-west4":
            return httpx.Response(200, json={"services": [service_json(far)]})
        if request.url.params.get("pageToken") == "next":
            return httpx.Response(200, json={"services": [service_json(second_page)]})
        return httpx.Response(
            200,
            json={
                "services": [
                    service_json(own),
                    service_json(conflict, deployment="deployment-b"),
                    service_json(foreign),
                    named("production"),
                    # Our name without our labels is never ours to delete.
                    named(session().app_name),
                ],
                "nextPageToken": "next",
            },
        )

    resources = await provider(handler).inventory()
    assert {r.session_id: r.region for r in resources} == {
        own.id: "us-central1",
        second_page.id: "us-central1",
        far.id: "europe-west4",
    }
    assert all(r.machine_id and r.deployment_id == "deployment-a" for r in resources)


class Routed(httpx.AsyncBaseTransport):
    """The Admin API on one transport and the session's own URL on another."""

    def __init__(self, admin, session_host, thin):
        self.admin, self.session_host, self.thin = admin, session_host, thin
        self.session_requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        if request.url.host == "run.googleapis.com":
            return await self.admin.handle_async_request(request)
        assert request.url.host == self.session_host
        self.session_requests.append(request)
        return await self.thin.handle_async_request(request)


async def test_call_sends_the_google_identity_beside_the_session_token():
    owned = session()
    thin = FastMCP("thin-test")
    admin_reads = 0

    @thin.tool
    async def run_bash(command: str, timeout_s: int):
        return {"exit_code": 0, "stdout": command, "timed_out": False}

    async def admin(request):
        nonlocal admin_reads
        admin_reads += 1
        return httpx.Response(200, json=service_json(owned))

    host = f"{owned.app_name}-abc123-uc.a.run.app"
    tokens = Tokens()
    app = thin.http_app(path="/mcp/", stateless_http=True, json_response=True)
    async with app.router.lifespan_context(app):
        transport = Routed(httpx.MockTransport(admin), host, httpx.ASGITransport(app=app))
        cloud_run = CloudRunComputeProvider(config(), transport=transport, tokens=tokens)
        for _ in range(2):
            result = await cloud_run.call(
                owned, "per-session-token", "run_bash", {"command": "hello", "timeout_s": 40}
            )
            assert result == {"exit_code": 0, "stdout": "hello", "timed_out": False}
    # The audience is the service URL, and the address is read from Cloud Run once.
    assert tokens.audiences == ["https://" + host] * 2
    assert admin_reads == 1
    assert transport.session_requests
    for request in transport.session_requests:
        assert request.url.path == "/mcp/"
        # Cloud Run checks the identity token; the session checks its own bearer token.
        assert request.headers["x-serverless-authorization"] == "Bearer google-identity-token"
        assert request.headers["authorization"] == "Bearer per-session-token"


@pytest.mark.parametrize("uri", ["", "http://x.a.run.app", "https://attacker.example", "::"])
async def test_call_refuses_an_address_that_is_not_a_cloud_run_url(uri):
    owned = session()

    async def handler(request):
        assert request.url.host == "run.googleapis.com", "the session token left for a bad host"
        return httpx.Response(200, json={**service_json(owned), "uri": uri})

    with pytest.raises(ComputeProviderError, match="did not answer"):
        await provider(handler).call(owned, "per-session-token", "run_bash", {})


@pytest.mark.parametrize("failure", ["refused", "broken"])
async def test_errors_never_carry_remote_bodies(capsys, failure):
    owned = session()

    async def handler(request):
        if failure == "broken":
            raise httpx.ConnectError("secret-detail google-access-token")
        return httpx.Response(403, json={"error": {"message": "secret-detail google-access-token"}})

    cloud_run = provider(handler)
    for operation in (
        lambda: cloud_run.ensure(owned, "machine-token"),
        cloud_run.inventory,
        lambda: cloud_run.delete(resource_of(owned)),
        lambda: cloud_run.call(owned, "machine-token", "run_bash", {}),
    ):
        with pytest.raises(ComputeProviderError) as error:
            await operation()
        assert "secret-detail" not in str(error.value)
        assert error.value.__cause__ is None
        assert error.value.__context__ is None or error.value.__suppress_context__
    logged = capsys.readouterr()
    assert ("status=403" in logged.out) == (failure == "refused")
    assert "secret-detail" not in logged.out + logged.err
    assert "google-access-token" not in logged.out + logged.err


async def test_an_unconfigured_provider_sends_nothing():
    async def unreachable(request):
        raise AssertionError("an unconfigured provider must not send requests")

    unconfigured = CloudRunComputeProvider(
        ComputeSettings(deployment_id="deployment-a"),
        transport=httpx.MockTransport(unreachable),
        tokens=Tokens(),
    )
    with pytest.raises(ComputeProviderError, match="not configured"):
        await unconfigured.inspect(session())


class FakeCredentials:
    def __init__(self) -> None:
        self.token: str | None = None
        self.refreshes = 0

    @property
    def valid(self) -> bool:
        return self.token is not None

    def refresh(self, request) -> None:
        self.refreshes += 1
        self.token = f"token-{self.refreshes}"


async def test_default_credentials_are_reused_until_expired_and_failures_are_sanitized(
    monkeypatch,
):
    access, identities = FakeCredentials(), {}

    def fetch(audience, request=None):
        return identities.setdefault(audience, FakeCredentials())

    monkeypatch.setattr(compute_cloud_run.google_auth, "default", lambda scopes: (access, None))
    monkeypatch.setattr(compute_cloud_run.google_id_token, "fetch_id_token_credentials", fetch)
    tokens = ApplicationDefaultTokens()
    assert [await tokens.access_token() for _ in range(2)] == ["token-1", "token-1"]
    assert await tokens.id_token("https://a.run.app") == "token-1"
    assert await tokens.id_token("https://a.run.app") == "token-1"
    assert await tokens.id_token("https://b.run.app") == "token-1"
    assert set(identities) == {"https://a.run.app", "https://b.run.app"}
    access.token = None  # google-auth reports an expired credential as not valid
    assert await tokens.access_token() == "token-2"

    def refuse(scopes):
        raise google_exceptions.DefaultCredentialsError("path /secrets/key.json unreadable")

    monkeypatch.setattr(compute_cloud_run.google_auth, "default", refuse)
    with pytest.raises(ComputeProviderError) as error:
        await ApplicationDefaultTokens().access_token()
    assert "key.json" not in str(error.value) and error.value.__cause__ is None
