"""ACA routing retains the same sandbox and refreshes credentials on retries."""

import json

import httpx
import pytest
from mux.drivers.selfhosted.config import HarnessConfig
from mux.drivers.selfhosted.transport import DirectTransport, sandbox_id


@pytest.mark.parametrize("session_id", ["one", "two"])
async def test_pool_routes_every_c3_operation_and_reconstructed_transport(session_id):
    requests = []
    tokens = []

    async def token():
        value = f"pool-token-{len(tokens)}"
        tokens.append(value)
        return value

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + tokens[-1]
        assert request.url.params["identifier"] == sandbox_id(session_id)
        assert request.url.path.startswith("/pool/v0/")
        assert "c3" not in json.loads(request.content or "{}")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for method, path, body in [
            ("POST", "/v0/turns", {"idempotency_key": "once"}),
            ("POST", "/v0/turns/turn/actions", {"decision": "allow"}),
            ("POST", "/v0/turns/turn/cancel", {}),
            ("GET", "/v0/sessions/session/events?after_seq=7&extra=a&extra=b&empty=", None),
            ("POST", "/v0/turns", {"idempotency_key": "once"}),
        ]:
            # Rebuilding the transport cannot lose the session mapping.
            transport = DirectTransport(
                client,
                base_url="https://pool.example/pool",
                token_provider=token,
                session_pool=True,
            )
            await transport.request(
                method,
                path,
                session_id=session_id,
                body=body,
                mcp_authorization="Bearer ephemeral" if path == "/v0/turns" else None,
            )
    assert len(tokens) == len(requests) == 5
    assert requests[3].url.params.multi_items() == [
        ("after_seq", "7"),
        ("extra", "a"),
        ("extra", "b"),
        ("empty", ""),
        ("identifier", sandbox_id(session_id)),
    ]
    assert requests[0].content == requests[-1].content
    assert all(
        ("X-Daimon-MCP-Authorization" in request.headers) == (request.url.path == "/pool/v0/turns")
        for request in requests
    )
    assert "ephemeral" not in repr(transport.__dict__)
    assert sandbox_id("one") != sandbox_id("two")


async def test_pool_poll_retry_refreshes_token_and_preserves_cursor():
    calls = []

    async def token():
        return f"token-{len(calls)}"

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"events": [], "complete": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = DirectTransport(
            client, base_url="https://pool.example", token_provider=token, session_pool=True
        )
        await transport.request("GET", "/v0/sessions/s/events?after_seq=13", session_id="s")
    assert [request.headers["Authorization"] for request in calls] == [
        "Bearer token-0",
        "Bearer token-1",
    ]
    assert calls[0].url == calls[1].url
    assert calls[1].url.params["after_seq"] == "13"


@pytest.mark.parametrize(
    "path",
    [
        "/v0/turns?identifier=foreign",
        "/v0/turns?Identifier=foreign",
        "https://elsewhere/v0/turns",
        "//elsewhere/v0/turns",
        "/other",
        "/v0/turns#fragment",
    ],
)
async def test_pool_rejects_injected_routing_before_any_io(path):
    async def token():
        pytest.fail("invalid routing must not acquire a token")

    async with httpx.AsyncClient() as client:
        transport = DirectTransport(
            client, base_url="https://pool.example", token_provider=token, session_pool=True
        )
        with pytest.raises(ValueError):
            await transport.request("POST", path, session_id="s")


@pytest.mark.parametrize(
    "extra",
    [
        {"auth_mode": "bearer_env", "auth_ref": "TOKEN"},
        {"auth_ref": "api://other"},
        {"auth_ref": "https://dynamicsessions.io/.default"},
        {"harness_url": "http://localhost:8080"},
        {
            "transport": "foundry_invocations",
            "agent_name": "harness",
            "project_endpoint": "https://project",
        },
    ],
)
def test_pool_configuration_refuses_wrong_audience_or_transport(extra):
    config = dict(
        harness_url="https://pool.example",
        auth_mode="entra",
        auth_ref="https://dynamicsessions.io",
        session_pool=True,
    )
    with pytest.raises(ValueError):
        HarnessConfig.model_validate(config | extra)


def test_pool_configuration_roundtrip_and_explicit_token_provider():
    config = HarnessConfig(
        harness_url="https://pool.example",
        auth_mode="entra",
        auth_ref="https://dynamicsessions.io",
        session_pool=True,
    )
    assert HarnessConfig.model_validate(config.model_dump()) == config
    assert not HarnessConfig(harness_url="https://direct.example", auth_ref="TOKEN").session_pool
    with pytest.raises(ValueError, match="refreshing Entra"):
        DirectTransport(
            httpx.AsyncClient(), base_url="https://pool.example", token="static", session_pool=True
        )


def test_pool_audience_requires_explicit_pool_routing():
    with pytest.raises(ValueError, match="requires session_pool"):
        HarnessConfig(
            harness_url="https://pool.example",
            auth_mode="entra",
            auth_ref="https://dynamicsessions.io",
        )


@pytest.mark.parametrize("session_id", ["", " ", "\t"])
@pytest.mark.parametrize("mode", ["direct", "pool", "foundry"])
async def test_empty_session_is_refused_before_token_or_http(session_id, mode):
    from mux.drivers.selfhosted.transport import FoundryInvocationsTransport

    calls = []

    async def token():
        calls.append("token")
        return "token"

    def respond(request):
        calls.append("http")
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = (
            FoundryInvocationsTransport(
                client, project_endpoint="https://project", agent_name="agent", token_provider=token
            )
            if mode == "foundry"
            else DirectTransport(
                client,
                base_url="https://harness",
                token_provider=token,
                session_pool=mode == "pool",
            )
        )
        with pytest.raises(ValueError, match="session id"):
            await transport.request("GET", "/v0/sessions/s/events", session_id=session_id)
    assert calls == []
