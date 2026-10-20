"""A per-turn MCP bearer is delivered in memory and never enters replay state."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from daimon_harness import service
from daimon_harness.credentials import VIRTUAL_ENV, mcp_headers, mcp_origin, turn_credentials
from daimon_harness.runtime import TurnContext
from daimon_harness.service import create_app
from mux.drivers.selfhosted.wire import TurnRequest
from pydantic import JsonValue

from .conftest import FakeModelAdapter, turn


@pytest.mark.parametrize("invocation", [False, True])
async def test_mcp_header_is_transient_and_never_journaled_or_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invocation: bool
) -> None:
    secret = "Bearer per-turn-private-value"
    monkeypatch.delenv(VIRTUAL_ENV, raising=False)
    monkeypatch.setenv("HARNESS_DAIMON_MCP_URL", "https://mcp.example/mcp")
    logged: list[str] = []
    monkeypatch.setattr(service.audit_logger, "info", logged.append)
    retained: list[dict[str, str]] = []

    class CredentialsModel(FakeModelAdapter):
        async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
            self.calls += 1
            assert mcp_headers(context.request.mcp_servers[0], context.credentials) == {
                "Authorization": secret
            }
            assert VIRTUAL_ENV not in os.environ
            retained.append(context.credentials)
            await context.emit(
                "model_request_started", {"request_id": "measured", "model": "gpt-5"}
            )
            await context.emit(
                "usage",
                {
                    "request_id": "measured",
                    "model": "gpt-5",
                    "input_tokens": 1,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "reasoning_tokens": 0,
                },
            )
            # An upstream echo must not turn the transport credential into persisted content.
            await context.emit(
                "agent_message",
                {"message_id": "msg-1", "parts": [{"type": "text", "text": secret}]},
            )
            return [{"role": "assistant", "content": secret}]

    model = CredentialsModel()
    app = create_app(home=tmp_path, token="test-token", adapter=model)
    body = turn()
    body["mcp_servers"] = [
        {
            "name": "daimon-mcp",
            "url": "https://mcp.example/mcp",
            "headers_env": {"Authorization": VIRTUAL_ENV},
        }
    ]
    path = "/invocations" if invocation else "/v0/turns"
    request = {"c3": {"method": "POST", "path": "/v0/turns", "body": body}} if invocation else body
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-token"},
        ) as client,
    ):
        missing = await client.post(path, json=request)
        assert missing.status_code == 422
        assert model.calls == 0
        assert app.state.supervisor.journal.find("turn-1") is None
        response = await client.post(
            path, json=request, headers={"X-Daimon-MCP-Authorization": secret}
        )
        assert response.status_code == 200
        assert "[redacted]" in response.text
        assert secret not in response.text
        replay = await client.post(path, json=request)
        assert replay.status_code == 200  # A replay needs no credential and performs no model I/O.
        assert replay.text == response.text
        assert model.calls == 1
        assert retained == [{}]
    for path in (tmp_path / ".daimon").rglob("*"):
        if path.is_file():
            assert b"per-turn-private-value" not in path.read_bytes(), path
    assert "per-turn-private-value" not in "\n".join(logged)
    assert VIRTUAL_ENV not in os.environ


@pytest.mark.parametrize(
    "server,url,reference,authorization",
    [
        ("other", "https://mcp.example/mcp", VIRTUAL_ENV, "Bearer test"),
        ("daimon-mcp", "https://other.example/mcp", VIRTUAL_ENV, "Bearer test"),
        ("daimon-mcp", "https://mcp.example/mcp", VIRTUAL_ENV, None),
        ("daimon-mcp", "https://mcp.example/mcp", "OTHER_ENV", "Bearer test"),
    ],
)
def test_reserved_mcp_credential_is_bound_to_configured_server(
    monkeypatch: pytest.MonkeyPatch,
    server: str,
    url: str,
    reference: str,
    authorization: str | None,
) -> None:
    monkeypatch.setenv("HARNESS_DAIMON_MCP_URL", "https://mcp.example/mcp")
    body = turn()
    body["mcp_servers"] = [
        {"name": server, "url": url, "headers_env": {"Authorization": reference}}
    ]
    with pytest.raises(ValueError):
        turn_credentials(TurnRequest.model_validate(body), authorization)


@pytest.mark.parametrize(
    "reference", ["AZURE_OPENAI_API_KEY", "HARNESS_TOKEN", "PATH", "OTHER_ENV"]
)
def test_environment_credentials_require_operator_prefix(monkeypatch, reference):
    from mux.drivers.selfhosted.wire import MCPServer

    monkeypatch.setenv(reference, "never-export-this")
    server = MCPServer(
        name="other", url="https://external.example/mcp", headers_env={"Authorization": reference}
    )
    request = TurnRequest.model_validate(turn())
    request.mcp_servers = [server]
    with pytest.raises(ValueError, match="HARNESS_MCP_CRED_"):
        turn_credentials(request, None)
    with pytest.raises(ValueError, match="HARNESS_MCP_CRED_"):
        mcp_headers(server, {})


@pytest.mark.parametrize(
    "url",
    [
        "http://external.example/mcp",
        "http://127.0.0.1.evil.example/mcp",
        "http://localhost.evil/mcp",
    ],
)
def test_non_loopback_credentials_require_https(monkeypatch, url):
    from mux.drivers.selfhosted.wire import MCPServer

    monkeypatch.setenv("HARNESS_MCP_CRED_TEST", "operator-value")
    server = MCPServer(
        name="other", url=url, headers_env={"Authorization": "HARNESS_MCP_CRED_TEST"}
    )
    with pytest.raises(ValueError, match="HTTPS"):
        mcp_headers(server, {})


@pytest.mark.parametrize(
    "url",
    [
        "https://external.example/mcp",
        "https://localhost/mcp",
        "https://127.0.0.1/mcp",
        "https://[::1]/mcp",
    ],
)
def test_operator_bindings_support_https_origins(monkeypatch, url):
    from mux.drivers.selfhosted.wire import MCPServer

    monkeypatch.setenv("HARNESS_MCP_CRED_TEST", "operator-value")
    server = MCPServer(
        name="other", url=url, headers_env={"Authorization": "HARNESS_MCP_CRED_TEST"}
    )
    request = TurnRequest.model_validate(turn())
    request.mcp_servers = [server]
    monkeypatch.setenv(
        "HARNESS_MCP_CRED_BINDINGS",
        json.dumps(
            [{"name": "HARNESS_MCP_CRED_TEST", "origin": mcp_origin(url), "tenants": ["*"]}]
        ),
    )
    resolved = turn_credentials(request, None)
    assert resolved == {"HARNESS_MCP_CRED_TEST": "operator-value"}
    assert mcp_headers(server, resolved) == {"Authorization": "operator-value"}


def test_daimon_url_normalization_and_reserved_alias(monkeypatch):
    from mux.drivers.selfhosted.wire import MCPServer

    monkeypatch.setenv("HARNESS_DAIMON_MCP_URL", "https://MCP.example:443/mcp/")
    request = TurnRequest.model_validate(turn())
    server = MCPServer(
        name="daimon-mcp", url="https://mcp.example/mcp", headers_env={"Authorization": VIRTUAL_ENV}
    )
    request.mcp_servers = [server]
    assert turn_credentials(request, "Bearer test") == {VIRTUAL_ENV: "Bearer test"}
    server.name = "alias"
    server.headers_env = {}
    with pytest.raises(ValueError, match="reserved server name"):
        turn_credentials(request, None)


def test_virtual_credential_never_falls_back_to_process_environment(monkeypatch):
    from mux.drivers.selfhosted.wire import MCPServer

    monkeypatch.setenv("HARNESS_DAIMON_MCP_URL", "https://mcp.example/mcp")
    monkeypatch.setenv(VIRTUAL_ENV, "must-not-resolve")
    server = MCPServer(
        name="daimon-mcp", url="https://mcp.example/mcp", headers_env={"Authorization": VIRTUAL_ENV}
    )
    with pytest.raises(KeyError):
        mcp_headers(server, {})


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example/mcp",
        "https://allowed.example:444/mcp",
        "https://allowed.example.attacker.test/mcp",
    ],
)
def test_bound_credentials_never_attach_to_another_origin(monkeypatch, url):
    from mux.drivers.selfhosted.wire import MCPServer

    monkeypatch.setenv("HARNESS_MCP_CRED_TEST", "private-value")
    monkeypatch.setenv(
        "HARNESS_MCP_CRED_BINDINGS",
        json.dumps(
            [
                {
                    "name": "HARNESS_MCP_CRED_TEST",
                    "origin": "https://allowed.example",
                    "tenants": ["*"],
                }
            ]
        ),
    )
    request = TurnRequest.model_validate(turn())
    server = MCPServer(
        name="remote", url=url, headers_env={"Authorization": "HARNESS_MCP_CRED_TEST"}
    )
    request.mcp_servers = [server]
    with pytest.raises(ValueError, match="different origin"):
        turn_credentials(request, None)
    with pytest.raises(ValueError, match="different origin"):
        mcp_headers(server, {"HARNESS_MCP_CRED_TEST": "private-value"})


def test_unbound_credentials_are_refused_and_same_origin_paths_are_allowed(monkeypatch):
    from mux.drivers.selfhosted.wire import MCPServer

    monkeypatch.setenv("HARNESS_MCP_CRED_TEST", "private-value")
    request = TurnRequest.model_validate(turn())
    server = MCPServer(
        name="remote",
        url="https://allowed.example:443/new/path",
        headers_env={"Authorization": "HARNESS_MCP_CRED_TEST"},
    )
    request.mcp_servers = [server]
    monkeypatch.setenv("HARNESS_MCP_CRED_BINDINGS", "[]")
    with pytest.raises(ValueError, match="unbound"):
        turn_credentials(request, None)
    monkeypatch.setenv(
        "HARNESS_MCP_CRED_BINDINGS",
        json.dumps(
            [
                {
                    "name": "HARNESS_MCP_CRED_TEST",
                    "origin": "https://allowed.example",
                    "tenants": ["11111111-1111-1111-1111-111111111111"],
                }
            ]
        ),
    )
    credentials = turn_credentials(request, None)
    assert mcp_headers(server, credentials) == {"Authorization": "private-value"}


async def test_mismatched_binding_refuses_turn_before_model_io(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MCP_CRED_TEST", "private-value")
    monkeypatch.setenv(
        "HARNESS_MCP_CRED_BINDINGS",
        json.dumps(
            [
                {
                    "name": "HARNESS_MCP_CRED_TEST",
                    "origin": "https://allowed.example",
                    "tenants": ["*"],
                }
            ]
        ),
    )
    adapter = FakeModelAdapter()
    monkeypatch.delenv("HARNESS_MCP_CRED_TEST")
    app = create_app(home=tmp_path, token="test", adapter=adapter)
    body = turn()
    body["mcp_servers"] = [
        {
            "name": "remote",
            "url": "https://attacker.example/mcp",
            "headers_env": {"X-Credential": "HARNESS_MCP_CRED_TEST"},
        }
    ]
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test"},
        ) as client,
    ):
        response = await client.post("/v0/turns", json=body)
        assert response.status_code == 422
        assert adapter.calls == 0
        assert "private-value" not in response.text


@pytest.mark.parametrize(
    "raw",
    [
        {},
        [{"name": "HARNESS_MCP_CRED_TEST", "origin": "http://localhost", "tenants": ["*"]}],
        [
            {
                "name": "HARNESS_MCP_CRED_TEST",
                "origin": "https://allowed.example/path",
                "tenants": ["*"],
            }
        ],
        [{"name": "HARNESS_MCP_CRED_TEST", "origin": "https://allowed.example", "tenants": []}],
        [
            {
                "name": "HARNESS_MCP_CRED_TEST",
                "origin": "https://allowed.example",
                "tenants": ["invalid"],
            }
        ],
        [{"name": "HARNESS_MCP_CRED_TEST", "origin": "https://allowed.example", "tenants": ["*"]}]
        * 2,
    ],
)
def test_invalid_operator_binding_configuration_refused(monkeypatch, raw):
    from daimon_harness.credentials import load_bindings

    monkeypatch.setenv("HARNESS_MCP_CRED_BINDINGS", json.dumps(raw))
    with pytest.raises(ValueError):
        load_bindings()


async def test_real_mcp_initialization_failure_logs_are_redacted(tmp_path, monkeypatch, caplog):
    import logging

    from daimon_harness import model_adapter
    from daimon_harness.model_adapter import AzureModelAdapter
    from daimon_harness.runtime import discard_event
    from mux.drivers.selfhosted.wire import MCPServer
    from pydantic import ValidationError

    secret = "Bearer synthetic-mcp-initialization-secret"
    monkeypatch.setenv("HARNESS_DAIMON_MCP_URL", "https://mcp.example/mcp")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test")
    request = TurnRequest.model_validate(turn())
    request.mcp_servers = [
        MCPServer(
            name="daimon-mcp",
            url="https://mcp.example/mcp",
            headers_env={"Authorization": VIRTUAL_ENV},
        )
    ]
    runtime = TurnContext(request, tmp_path, discard_event, credentials={VIRTUAL_ENV: secret})

    def invalid_response(request):
        assert request.headers["Authorization"] == secret
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "result": {"echo": secret},
            },
        )

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(invalid_response), **kwargs)

    monkeypatch.setattr(model_adapter, "bound_mcp_http_client", client_factory)
    with caplog.at_level(logging.WARNING), pytest.raises(ValidationError):
        await AzureModelAdapter().run(runtime, [])
    assert caplog.records
    assert "synthetic-mcp-initialization-secret" not in caplog.text
    assert "[redacted]" in caplog.text


def test_log_redaction_covers_other_turns_and_exception_tracebacks(caplog):
    import logging

    from daimon_harness.credentials import credential_log_redaction

    with (
        credential_log_redaction({"a": "first-secret"}),
        credential_log_redaction({"b": "Bearer second-secret"}),
    ):
        try:
            raise ValueError("first-secret second-secret")
        except ValueError:
            logging.getLogger("sdk.failure").exception("response: %s", "Bearer second-secret")
    assert "first-secret" not in caplog.text and "second-secret" not in caplog.text
    assert "ValueError" in caplog.text and "[redacted]" in caplog.text


@pytest.mark.parametrize("auth_mode", ["key", "bearer"])
async def test_sdk_debug_logs_keep_header_filters_and_hide_all_secrets(
    tmp_path, monkeypatch, caplog, auth_mode
):
    import logging

    from daimon_harness import model_adapter
    from daimon_harness.model_adapter import AzureModelAdapter
    from daimon_harness.runtime import discard_event
    from openai import AsyncAzureOpenAI

    key = "synthetic-azure-debug-key-67241"
    bearer = "synthetic-azure-debug-bearer-67241"
    harness = "synthetic-harness-debug-token-67241"
    operator = "synthetic-unused-mcp-debug-secret-67241"
    client_secret = "synthetic-identity-debug-secret-67241"
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://unit-test.openai.azure.com")
    monkeypatch.setenv("HARNESS_TOKEN", harness)
    monkeypatch.setenv("HARNESS_MCP_CRED_UNUSED", operator)
    monkeypatch.setenv("AZURE_CLIENT_SECRET", client_secret)
    if auth_mode == "key":
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", key)
    else:
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)

    async def token_provider():
        return bearer

    monkeypatch.setattr(model_adapter, "get_bearer_token_provider", lambda *args: token_provider)
    filtered_headers = []

    class AfterSDKFilter(logging.Filter):
        def filter(self, record):
            if isinstance(record.args, dict) and "headers" in record.args:
                filtered_headers.append(record.args["headers"].copy())
            return True

    probe = AfterSDKFilter()
    sdk_logger = logging.getLogger("openai._base_client")
    sdk_logger.addFilter(probe)

    def respond(request):
        expected = {"api-key": key} if auth_mode == "key" else {"Authorization": f"Bearer {bearer}"}
        for name, value in expected.items():
            assert request.headers[name] == value
        logging.getLogger("sdk.echo").warning(
            "credentials: %s",
            [harness, operator, client_secret, key if auth_mode == "key" else bearer],
        )
        return httpx.Response(
            200,
            json={
                "id": "response-debug",
                "object": "response",
                "created_at": 1,
                "model": "gpt-5",
                "status": "completed",
                "output": [
                    {
                        "id": "message-debug",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "done", "annotations": []}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
            },
        )

    def factory(**kwargs):
        return AsyncAzureOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        )

    monkeypatch.setattr(model_adapter, "AsyncAzureOpenAI", factory)
    adapter = AzureModelAdapter()
    try:
        with caplog.at_level(logging.DEBUG):
            await adapter.run(
                TurnContext(TurnRequest.model_validate(turn()), tmp_path, discard_event), []
            )
    finally:
        sdk_logger.removeFilter(probe)
        if adapter.credential is not None:
            await adapter.credential.close()
    assert "Request options:" in caplog.text
    assert filtered_headers
    header_name = "api-key" if auth_mode == "key" else "Authorization"
    assert any(headers.get(header_name) == "<redacted>" for headers in filtered_headers)
    for secret in (key, bearer, harness, operator, client_secret):
        assert secret not in caplog.text
    assert "[redacted]" in caplog.text
