"""An MCP tool error result reaches the model as an error tool result with its content."""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import subprocess

import httpx
import pytest
import uvicorn
from daimon_harness import model_adapter
from daimon_harness.credentials import turn_credentials
from daimon_harness.model_adapter import AzureModelAdapter
from daimon_harness.runtime import TurnContext, discard_event
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mux.drivers.selfhosted.wire import MCPServer, TurnRequest
from openai import AsyncAzureOpenAI

from .conftest import turn

GUIDANCE = "step_required: call prepare first, then retry with its id"


async def test_mcp_error_result_content_reaches_model(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from sse_starlette.sse import AppStatus

    monkeypatch.setattr(AppStatus, "should_exit", False)
    monkeypatch.setattr(AppStatus, "enable_automatic_graceful_drain", False)
    secret = "Bearer synthetic-error-path-secret"
    monkeypatch.setenv("HARNESS_MCP_CRED_SAMPLE", secret)
    mcp = FastMCP("unit-test", stateless_http=True)

    @mcp.tool()
    def refuse() -> str:
        # The error text also echoes the credential, which must stay redacted.
        raise ToolError(f"{GUIDANCE} ({secret})")

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
            mcp.streamable_http_app(),
            log_level="error",
            ssl_certfile=str(certificate),
            ssl_keyfile=str(private_key),
        )
    )

    def local_tls_client(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            verify=ssl.create_default_context(cafile=str(certificate)),
            follow_redirects=False,
        )

    monkeypatch.setattr(model_adapter, "bound_mcp_http_client", local_tls_client)
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    bodies: list[dict[str, object]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            output: list[dict[str, object]] = [
                {
                    "type": "function_call",
                    "id": "fc",
                    "call_id": "mcp-call",
                    "name": "sample__refuse",
                    "arguments": "{}",
                    "status": "completed",
                }
            ]
        else:
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
                "id": f"response-{len(bodies)}",
                "object": "response",
                "created_at": 1,
                "model": "gpt-6-luna",
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
    request = TurnRequest.model_validate(turn())
    request.tool_policy = {"sample/refuse": "auto"}
    request.mcp_servers = [
        MCPServer(
            name="sample",
            url=f"https://127.0.0.1:{port}/mcp",
            headers_env={"Authorization": "HARNESS_MCP_CRED_SAMPLE"},
        )
    ]
    context = TurnContext(
        request, tmp_path, discard_event, credentials=turn_credentials(request, None)
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def emit(kind: str, data: dict[str, object]) -> None:
        events.append((kind, data))

    context.emit = emit
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        await AzureModelAdapter().run(context, [])
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()

    results = [data for kind, data in events if kind == "tool_result"]
    assert len(results) == 1 and results[0]["is_error"] is True
    assert GUIDANCE in str(results[0]["output"])
    second = str(bodies[1]["input"])
    assert GUIDANCE in second
    assert "Tool failed" not in second
    assert "synthetic-error-path-secret" not in second
    assert "synthetic-error-path-secret" not in str(events)
