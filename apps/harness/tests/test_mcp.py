"""The real SDK streamable HTTP client calls a local MCP server after approval."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn
from daimon_harness import model_adapter
from daimon_harness.model_adapter import AzureModelAdapter
from daimon_harness.runtime import TurnContext
from fastmcp import FastMCP
from mux.drivers.selfhosted.wire import ActionRequest, MCPServer, TurnRequest
from openai import AsyncAzureOpenAI

from .conftest import turn


async def test_mcp_tool_executes_after_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FastMCP("unit-test")
    executed: list[str] = []

    @mcp.tool
    def echo(text: str) -> str:
        executed.append(text)
        return text

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(mcp.http_app(path="/mcp", stateless_http=True), log_level="error")
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if calls == 1:
            assert "sample__echo" in [tool["name"] for tool in body["tools"]]
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
    request.mcp_servers = [MCPServer(name="sample", url=f"http://127.0.0.1:{port}/mcp")]
    context = TurnContext(request, tmp_path, lambda kind, data: None)

    def emit(kind: str, data: dict[str, object]) -> None:
        events.append((kind, data))
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
        await AzureModelAdapter().run(context, [])
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
