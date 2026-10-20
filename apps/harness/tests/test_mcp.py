"""The real SDK streamable HTTP client calls a local MCP server after approval."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
from pathlib import Path

import httpx
import pytest
import uvicorn
from daimon_harness import model_adapter
from daimon_harness.credentials import turn_credentials
from daimon_harness.journal import Journal
from daimon_harness.model_adapter import AzureModelAdapter
from daimon_harness.runtime import TurnContext, discard_event
from fastmcp import FastMCP
from mux.drivers.selfhosted.wire import ActionRequest, MCPServer, TurnRequest
from openai import AsyncAzureOpenAI

from .conftest import turn


async def test_mcp_tool_executes_after_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "Bearer synthetic-operator-mcp-secret"
    monkeypatch.setenv("HARNESS_MCP_CRED_SAMPLE", secret)
    journal = Journal(tmp_path)
    mcp = FastMCP("unit-test")
    executed: list[str] = []

    @mcp.tool
    def echo(text: str) -> str:
        executed.append(text)
        return text + " " + secret

    @mcp.tool
    def forbidden() -> str:
        raise AssertionError("denied tool must not execute")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    certificate, private_key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            mcp.http_app(path="/mcp", stateless_http=True),
            log_level="error",
            ssl_certfile=str(certificate),
            ssl_keyfile=str(private_key),
        )
    )

    def local_tls_client(headers=None, timeout=None, auth=None):
        import ssl

        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            verify=ssl.create_default_context(cafile=str(certificate)),
            follow_redirects=False,
        )

    monkeypatch.setattr(model_adapter, "bound_mcp_http_client", local_tls_client)
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if calls == 1:
            assert "sample__echo" in [tool["name"] for tool in body["tools"]]
            assert "sample__forbidden" not in [tool["name"] for tool in body["tools"]]
            output = [
                {
                    "type": "function_call",
                    "id": "fc",
                    "call_id": "mcp-call",
                    "name": "sample__echo",
                    "arguments": '{"text":"mcp-result"}',
                    "status": "completed",
                }
            ]
        else:
            assert "mcp-result" in str(body["input"])
            assert "[redacted]" in str(body["input"])
            assert "synthetic-operator-mcp-secret" not in str(body)
            output = [
                {
                    "type": "message",
                    "id": "msg",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "done", "annotations": []}],
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": f"mcp-response-{calls}",
                "object": "response",
                "created_at": 1,
                "model": "gpt-5",
                "status": "completed",
                "output": output,
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 2,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 12,
                },
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
            },
        )

    azure = AsyncAzureOpenAI(
        azure_endpoint="https://unit-test.openai.azure.com",
        api_version="2025-04-01-preview",
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        max_retries=0,
    )
    monkeypatch.setattr(model_adapter, "AsyncAzureOpenAI", lambda **kwargs: azure)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://unit-test.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test")
    events: list[tuple[str, object]] = []
    request = TurnRequest.model_validate(turn())
    request.tool_policy = {"sample/*": "deny", "sample/echo": "ask"}
    request.mcp_servers = [
        MCPServer(
            name="sample",
            url=f"https://127.0.0.1:{port}/mcp",
            headers_env={"Authorization": "HARNESS_MCP_CRED_SAMPLE"},
        )
    ]
    monkeypatch.setenv(
        "HARNESS_MCP_CRED_BINDINGS",
        json.dumps(
            [
                {
                    "name": "HARNESS_MCP_CRED_SAMPLE",
                    "origin": f"https://127.0.0.1:{port}",
                    "tenants": ["*"],
                }
            ]
        ),
    )
    context = TurnContext(
        request, tmp_path, discard_event, credentials=turn_credentials(request, None)
    )

    async def emit(kind: str, data: dict[str, object]) -> None:
        events.append((kind, data))
        journal.append(request, kind, data)
        if kind == "requires_action":
            assert executed == []
            action = str(data["action_id"])
            context.pending[action].set_result(ActionRequest(action_id=action, decision="allow"))

    context.emit = emit
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        history = await AzureModelAdapter().run(context, [])
        journal.save_history(request.session_id, history)
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
    assert executed == ["mcp-result"]
    tool_use = next(data for kind, data in events if kind == "tool_use")
    assert tool_use == {
        "call_id": "mcp-call",
        "name": "sample/echo",
        "input": {"text": "mcp-result"},
        "executor": "mcp",
    }
    assert sum(kind == "requires_action" for kind, _ in events) == 1
    assert sum(kind == "usage" for kind, _ in events) == 2

    journal.close()
    for path in (tmp_path / ".daimon").rglob("*"):
        if path.is_file():
            assert b"synthetic-operator-mcp-secret" not in path.read_bytes()
    assert "synthetic-operator-mcp-secret" not in str(events)


async def test_credential_http_client_does_not_follow_redirects(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://attacker.example/mcp"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        model_adapter.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(redirect), **kwargs),
    )
    client = model_adapter.bound_mcp_http_client(headers={"X-Credential": "synthetic-secret"})
    async with client:
        response = await client.get("https://allowed.example/mcp")
    assert response.status_code == 307
    assert len(requests) == 1
    assert requests[0].url.host == "allowed.example"
