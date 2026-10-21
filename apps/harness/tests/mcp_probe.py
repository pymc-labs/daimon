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
from mcp.server.fastmcp import FastMCP
from mux.drivers.selfhosted.wire import ActionRequest, MCPServer, TurnRequest
from openai import AsyncAzureOpenAI

from .conftest import turn


async def mcp_turn_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, interop: bool = False
) -> None:
    from sse_starlette.sse import AppStatus

    # sse-starlette retains a process-wide shutdown flag after a previous uvicorn
    # fixture stops. Each fixture owns a new server lifetime on the session loop.
    monkeypatch.setattr(AppStatus, "should_exit", False)
    secret = "Bearer synthetic-operator-mcp-secret"
    monkeypatch.setenv("HARNESS_MCP_CRED_SAMPLE", secret)
    journal = Journal(tmp_path)
    mcp = FastMCP("unit-test", stateless_http=True)
    executed: list[str] = []

    @mcp.tool()
    def echo(text: str) -> str:
        executed.append(text)
        return text if interop else text + " " + secret

    @mcp.tool()
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
            mcp.streamable_http_app(),
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
    http_harness_client: httpx.AsyncClient | None = None
    http_session_id: str | None = None
    request_ids_at_io: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if interop:
            assert http_harness_client is not None and http_session_id is not None
            # Observe committed events through the real HTTP service at model I/O,
            # before the synthetic response can supply any usage for this request.
            durable = await http_harness_client.get(f"/v0/sessions/{http_session_id}/events")
            durable.raise_for_status()
            inventory = durable.json()["events"]
            starts = [event for event in inventory if event["type"] == "model_request_started"]
            measured = [event for event in inventory if event["type"] == "usage"]
            assert len(starts) == calls, "each model request must be journaled before model I/O"
            assert len(measured) == calls - 1
            assert [event["data"]["request_id"] for event in measured] == request_ids_at_io
            request_id = starts[-1]["data"]["request_id"]
            assert request_id not in request_ids_at_io
            request_ids_at_io.append(request_id)
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
            if interop:
                output.append(
                    {
                        "type": "function_call",
                        "id": "denied-fc",
                        "call_id": "denied-call",
                        "name": "sample__forbidden",
                        "arguments": "{}",
                        "status": "completed",
                    }
                )
        else:
            assert "mcp-result" in str(body["input"])
            if not interop:
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
        if interop:
            from mux.contracts.actions import UserToolConfirmation

            from .conftest import serve_harness
            from .interop_support import SCOPE, collect, driver_session, message

            # The synthetic operator credential remains confined to the HTTPS MCP fixture;
            # the driver can configure a credential-free public MCP connection here.
            # Startup captures the same operator binding used by direct SDK coverage.
            request.mcp_servers[0].headers_env = {}
            sdk = AzureModelAdapter()
            monkeypatch.delenv("AZURE_OPENAI_API_KEY")
            monkeypatch.delenv("HARNESS_MCP_CRED_SAMPLE")

            class FakeResponsesAdapter:
                # Azure HTTP is replaced above by the owned synthetic-response client.
                # No service or model credentials are configured in test mode.
                async def run(self, context, history):
                    return await sdk.run(context, history)

            async with serve_harness(tmp_path / "http-harness", FakeResponsesAdapter()) as running:
                client, _, _ = running
                driver, session = await driver_session(
                    client,
                    mcp_servers=[
                        {
                            "name": "sample",
                            "url": f"https://127.0.0.1:{port}/mcp",
                            "tool_policy": {"allowed_tools": ["echo"], "echo": "ask"},
                        }
                    ],
                )
                http_harness_client, http_session_id = client, session.ref.id
                try:
                    sent = await driver.events.send(
                        SCOPE, session.ref, [message("mcp")], key="send"
                    )
                    async with asyncio.timeout(10):
                        while True:
                            snapshot = await driver.events.reconcile(SCOPE, session.ref)
                            if snapshot.state == "requires_action":
                                break
                            assert snapshot.state != "idle", "model failed before MCP approval"
                            await asyncio.sleep(0.01)
                    assert executed == []
                    action = snapshot.required_actions[0]
                    assert action.payload["name"] == "sample/echo"
                    await driver.events.send(
                        SCOPE,
                        session.ref,
                        [
                            UserToolConfirmation(
                                action_id=action.id,
                                decision="allow",
                            )
                        ],
                        key="approve",
                        expected_turn=sent.turn_id,
                    )
                    recorded = await collect(driver, session, after="0")
                    assert recorded[-1].payload["outcome"] == "completed"
                    denied = [
                        event
                        for event in recorded
                        if event.type == "agent.tool_result"
                        and event.payload["call_id"] == "denied-call"
                    ]
                    assert len(denied) == 1 and denied[0].payload["is_error"]
                    assert "denied" in str(denied[0].payload["content"]).lower()
                    assert executed == ["mcp-result"]
                    usage = await driver.usage.reconcile(SCOPE, session.ref)
                    assert calls == 2
                    assert len(usage) == calls
                    assert all(item.completeness == "measured" for item in usage)
                    durable = await client.get(f"/v0/sessions/{session.ref.id}/events")
                    durable.raise_for_status()
                    inventory = durable.json()["events"]
                    starts = [
                        event for event in inventory if event["type"] == "model_request_started"
                    ]
                    measured = [event for event in inventory if event["type"] == "usage"]
                    assert len(starts) == len(measured) == calls
                    assert [event["data"]["request_id"] for event in starts] == request_ids_at_io
                    assert [event["data"]["request_id"] for event in measured] == request_ids_at_io
                    assert [item.native_meter["request_id"] for item in usage] == request_ids_at_io
                    assert all(
                        start["seq"] < end["seq"]
                        for start, end in zip(starts, measured, strict=True)
                    )
                    assert (
                        sum(
                            event.type == "native.selfhosted.model_request_started"
                            for event in recorded
                        )
                        == calls
                    )
                finally:
                    await driver.aclose()
        else:
            history = await AzureModelAdapter().run(context, [])
            journal.save_history(request.session_id, history)
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
    if interop:
        journal.close()
        return
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
