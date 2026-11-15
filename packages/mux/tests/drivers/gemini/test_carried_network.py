"""A session's own egress limits reach Gemini; header transforms outside a binding refuse."""

import json

import httpx
import pytest
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from mux.contracts.ids import Scope
from mux.contracts.resources import MCPConnection
from mux.drivers.gemini.mcp_auth import (
    MCPSecretResolver,
    Object,
    RedactedCredential,
    authenticated_template,
    compile_mcp,
    refresh_bound_request,
)
from mux.drivers.gemini.transport import SDKTransport
from mux.errors import UnsupportedCapability
from pydantic import JsonValue

SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="allowed")
URL = "https://mcp.example.com/mcp"
LIMITED: JsonValue = {"allowlist": [{"domain": "pypi.org"}, {"domain": "files.example.com"}]}
BOUND = MCPConnection(
    name="docs", url=URL, credential_ref="host:token", tool_policy={"allowed_tools": ["search"]}
)
NETWORKS = [pytest.param("disabled", id="disabled"), pytest.param(LIMITED, id="allowlist")]


async def resolve(scope: Scope, ref: str, destination: str) -> str:
    return "bearer-fixture"


def request(network: JsonValue, tools: list[JsonValue] | None = None) -> Object:
    environment: Object = {"type": "remote", "sources": [], "network": network}
    return {
        "agent": "a",
        "agent_config": {"model": "m"},
        "environment": environment,
        "tools": tools or [],
    }


def network_of(sent: Object) -> JsonValue:
    environment = sent["environment"]
    assert isinstance(environment, dict), "the environment stays inline"
    return environment.get("network")


@pytest.mark.parametrize("network", NETWORKS)
@pytest.mark.parametrize(
    "resolver", [pytest.param(None, id="shared_thread"), pytest.param(resolve, id="no_mcp")]
)
async def test_refresh_when_no_mcp_server_then_network_passes_unchanged(
    network: JsonValue, resolver: MCPSecretResolver | None
) -> None:
    expected = request(network)
    sent = await authenticated_template(SCOPE, expected, expected, (), resolver, overrides={})
    fresh = await refresh_bound_request(sent)
    assert network_of(fresh) == network, "an egress limit carries no credential, so it is sent"


@pytest.mark.parametrize("network", NETWORKS)
async def test_refresh_when_mcp_server_has_no_credential_then_network_passes(
    network: JsonValue,
) -> None:
    server = (MCPConnection(name="docs", url=URL),)
    expected = request(network, compile_mcp(server, enabled=True))
    sent = await authenticated_template(SCOPE, expected, expected, server, resolve, overrides={})
    fresh = await refresh_bound_request(sent)
    assert network_of(fresh) == network, "an unbound server leaves the limit as compiled"


@pytest.mark.parametrize("network", NETWORKS)
async def test_refresh_when_mcp_server_is_bound_then_network_survives_reminting(
    network: JsonValue,
) -> None:
    server = (BOUND,)
    expected = request(network, compile_mcp(server, enabled=True))
    sent = await authenticated_template(SCOPE, expected, expected, server, resolve, overrides={})
    fresh = await refresh_bound_request(sent)
    assert network_of(fresh) == network, "the binding keeps the compiled limit"


TRANSFORMED = [
    pytest.param(
        {
            "allowlist": [
                {"domain": "files.example.com", "transform": {"Authorization": "Bearer x"}}
            ]
        },
        id="raw_transform",
    ),
    pytest.param(
        {
            "allowlist": [
                {
                    "domain": "storage.googleapis.com",
                    "transform": {"Authorization": RedactedCredential("Bearer stale")},
                }
            ]
        },
        id="replayed_mount_rule",
    ),
    pytest.param({"allowlist": [{"domain": "pypi.org", "path": "/"}]}, id="extra_rule_key"),
    pytest.param({"allowlist": [{"domain": "pypi.org"}], "mode": "x"}, id="extra_network_key"),
    pytest.param({"allowlist": "pypi.org"}, id="rules_not_a_list"),
    pytest.param({"allowlist": [{"domain": ""}]}, id="empty_domain"),
    pytest.param("enabled", id="unknown_mode"),
]


@pytest.mark.parametrize("network", TRANSFORMED)
async def test_refresh_when_unbound_network_is_not_a_plain_limit_then_refuses(
    network: JsonValue,
) -> None:
    with pytest.raises(UnsupportedCapability) as refused:
        await refresh_bound_request(request(network))
    assert refused.value.missing == ("unbound_gcs_authentication",), "a transform never rides along"


async def test_refresh_when_bound_request_carries_a_transform_then_refuses() -> None:
    server = (BOUND,)
    rule: JsonValue = {"domain": "files.example.com", "transform": {"Authorization": "Bearer x"}}
    expected = request({"allowlist": [rule]}, compile_mcp(server, enabled=True))
    sent = await authenticated_template(SCOPE, expected, expected, server, resolve, overrides={})
    with pytest.raises(UnsupportedCapability) as refused:
        await refresh_bound_request(sent)
    assert refused.value.missing == ("unbound_gcs_authentication",), "only the mount mints one"


async def test_create_when_network_is_limited_then_the_api_receives_it() -> None:
    bodies: list[Object] = []

    def respond(sent: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(sent.content))
        return httpx.Response(200, json={"id": "native", "status": "in_progress"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        await SDKTransport(client).create({**request(LIMITED), "input": []})
        await client.aio.aclose()
    assert [network_of(body) for body in bodies] == [LIMITED], "the allowlist is on the wire"
