"""Provider boundary tests independent of binary availability."""

import asyncio
import json

import httpx
import pytest
from daimon_harness.codex_adapter import CodexConnection, private_config
from daimon_harness.codex_relay import ResponsesRelay
from daimon_harness.errors import HarnessError
from daimon_harness.runtime import TurnContext
from mux.drivers.selfhosted.wire import TurnRequest

from .conftest import turn
from .test_codex_adapter import assistant, sse_response, write_call


async def exercise(tmp_path, respond, emit=None):
    events = []

    async def record(kind, data):
        events.append((kind, data))
        if emit is not None:
            await emit(kind, data)

    async def headers():
        return {"api-key": "synthetic-azure-key"}

    context = TurnContext(TurnRequest.model_validate(turn()), tmp_path, record)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        relay = ResponsesRelay(
            context,
            [],
            [{"name": "harness_write", "type": "function"}],
            client,
            "https://fake.openai.azure.com/openai/responses",
            headers,
        )
        async with relay.serve() as endpoint, httpx.AsyncClient() as caller:
            response = await caller.post(
                endpoint + "/responses",
                headers={"Authorization": "Bearer " + relay.capability},
                json={
                    "model": "gpt-6-luna",
                    "input": [],
                    "tools": [{"name": "exec_command"}],
                    "store": True,
                    "previous_response_id": "untrusted",
                },
            )
        return relay, events, response


@pytest.mark.parametrize(
    "status,usage,expected,measured",
    [
        ("completed", False, "usage_missing", False),
        ("incomplete", True, "model_incomplete", True),
        ("failed", True, "model_failed", True),
        ("completed", True, None, True),
    ],
)
async def test_actual_provider_usage_and_incomplete_tools(
    tmp_path, status, usage, expected, measured
):
    def respond(request):
        assert request.headers["api-key"] == "synthetic-azure-key"
        body = json.loads(request.content)
        assert body["model"] == "gpt-6-luna" and body["store"] is False
        assert body["tools"] == [{"name": "harness_write", "type": "function"}]
        assert "previous_response_id" not in body
        return sse_response([write_call()], status=status, usage=usage)

    relay, events, response = await exercise(tmp_path, respond)
    assert (relay.failure.kind if relay.failure else None) == expected
    assert events[0][0] == "model_request_started"
    assert sum(kind == "usage" for kind, _ in events) == measured
    assert sum(kind == "error" for kind, _ in events) == (not measured)
    assert events[-1][1]["request_id"] == events[0][1]["request_id"]
    assert ("function_call" in response.text) == (expected is None)


async def test_unregistered_native_call_refused_but_measured(tmp_path):
    native = {**write_call(), "name": "exec_command"}
    relay, events, response = await exercise(tmp_path, lambda _: sse_response([native]))
    assert relay.failure.kind == "codex_native_tool"
    assert [kind for kind, _ in events] == ["model_request_started", "usage"]
    assert "exec_command" not in response.text


async def test_http_failure_is_one_unknown_observation(tmp_path):
    relay, events, response = await exercise(tmp_path, lambda _: httpx.Response(401))
    assert response.status_code == 502
    assert [kind for kind, _ in events] == ["model_request_started", "error"]
    assert events[-1][1]["kind"] == "usage_unavailable"
    assert relay.failure is not None


async def test_durability_failure_prevents_model_io(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return sse_response([assistant()])

    async def emit(kind, data):
        raise HarnessError("durability_unavailable", "Synthetic journal failure")

    _, _, response = await exercise(tmp_path, respond, emit)
    assert response.status_code == 500
    assert calls == []


async def test_missing_capability_never_starts_a_model_request(tmp_path):
    events = []

    async def emit(kind, data):
        events.append(kind)

    async def headers():
        raise AssertionError("unauthorized request acquired credentials")

    async with httpx.AsyncClient() as upstream:
        context = TurnContext(TurnRequest.model_validate(turn()), tmp_path, emit)
        relay = ResponsesRelay(context, [], [], upstream, "https://fake", headers)
        async with relay.serve() as endpoint, httpx.AsyncClient() as caller:
            for token in ["", "Bearer wrong"]:
                response = await caller.post(
                    endpoint + "/responses", headers={"Authorization": token}, json={}
                )
                assert response.status_code == 401
        assert not events


async def test_native_approval_is_declined_without_execution(tmp_path):
    class Writer:
        def __init__(self):
            self.messages = []

        def write(self, data):
            self.messages.append(json.loads(data))

        async def drain(self):
            pass

    class Process:
        stdin = Writer()

    process = Process()
    context = TurnContext(TurnRequest.model_validate(turn()), tmp_path, lambda *_: None)
    connection = CodexConnection(process, context, {})
    for method in ["item/commandExecution/requestApproval", "item/fileChange/requestApproval"]:
        with pytest.raises(HarnessError, match="refused"):
            await connection.event({"id": 10, "method": method, "params": {}})
        assert process.stdin.messages[-1]["result"] == {"decision": "decline"}
    assert context.tool_calls == 0


async def test_cancel_inflight_model_records_unknown_and_closes_handler(tmp_path):
    started = asyncio.Event()

    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b""

    events = []

    async def emit(kind, data):
        events.append((kind, data))

    async def headers():
        return {}

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Waiting()))
        ) as upstream,
        httpx.AsyncClient() as caller,
    ):
        context = TurnContext(TurnRequest.model_validate(turn()), tmp_path, emit)
        relay = ResponsesRelay(context, [], [], upstream, "https://fake", headers)
        async with relay.serve() as endpoint:
            pending = asyncio.create_task(
                caller.post(
                    endpoint + "/responses",
                    headers={"Authorization": "Bearer " + relay.capability},
                    json={"input": []},
                )
            )
            await asyncio.wait_for(started.wait(), 5)
        await asyncio.gather(pending, return_exceptions=True)
        assert not relay.handlers
    assert [kind for kind, _ in events] == ["model_request_started", "error"]
    assert events[-1][1]["kind"] == "usage_unavailable"
    assert events[-1][1]["request_id"] == events[0][1]["request_id"]


def test_private_capability_file(tmp_path):
    private_config(tmp_path, "http://127.0.0.1:1", "synthetic-relay-capability", "gpt-6-luna")
    assert (tmp_path / "config.toml").stat().st_mode & 0o777 == 0o600
    assert "AZURE" not in (tmp_path / "config.toml").read_text()


async def test_cancel_during_usage_commit_does_not_emit_unknown(tmp_path):
    committing, release = asyncio.Event(), asyncio.Event()
    events = []

    async def emit(kind, data):
        if kind == "usage":
            committing.set()
            await release.wait()
        events.append((kind, data))

    async def headers():
        return {}

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: sse_response([assistant()]))
        ) as upstream,
        httpx.AsyncClient() as caller,
    ):
        context = TurnContext(TurnRequest.model_validate(turn()), tmp_path, emit)
        relay = ResponsesRelay(context, [], [], upstream, "https://fake", headers)
        async with relay.serve() as endpoint:
            pending = asyncio.create_task(
                caller.post(
                    endpoint + "/responses",
                    headers={"Authorization": "Bearer " + relay.capability},
                    json={"input": []},
                )
            )
            await asyncio.wait_for(committing.wait(), 5)
            for handler in relay.handlers:
                handler.cancel()
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
    assert [kind for kind, _ in events] == ["model_request_started", "usage"]


def test_real_tool_uid_relay_boundary():
    import os
    import subprocess
    import sys
    from pathlib import Path

    if os.geteuid() != 0:
        pytest.skip("run codex_relay_probe.py in a root subprocess to verify service/tool UIDs")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("codex_relay_probe.py"))],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert '"relay_isolated": true' in result.stdout


@pytest.mark.parametrize("collision", [False, True])
async def test_mcp_policy_mapping_keeps_original_server_tool_identity(
    tmp_path, monkeypatch, collision
):
    from contextlib import AsyncExitStack
    from types import SimpleNamespace

    from daimon_harness import model_adapter

    class Server:
        def __init__(self, name, **kwargs):
            self.name = name

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_tools(self):
            name = "b__echo" if self.name == "a" else ("echo" if collision else "other")
            return [
                SimpleNamespace(
                    name=name,
                    description="echo",
                    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                )
            ]

    monkeypatch.setattr(model_adapter, "MCPServerStreamableHttp", Server)
    request = TurnRequest.model_validate(
        {
            **turn(),
            "mcp_servers": [
                {"name": name, "url": "http://127.0.0.1:1/mcp", "headers_env": {}}
                for name in ["a", "a__b"]
            ],
            "tool_policy": {"a/b__echo": "deny"},
        }
    )
    context = TurnContext(request, tmp_path, lambda *_: None)
    policy_names = {}
    async with AsyncExitStack() as stack:
        if collision:
            with pytest.raises(HarnessError, match="aliases collide"):
                await model_adapter.context_tools(
                    context, stack, include_denied=True, policy_names=policy_names
                )
        else:
            await model_adapter.context_tools(
                context, stack, include_denied=True, policy_names=policy_names
            )
            assert policy_names["a__b__echo"] == "a/b__echo"
            assert context.permission(policy_names["a__b__echo"]) == "deny"
