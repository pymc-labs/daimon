"""Exercise the real Agents SDK loop with Azure HTTP mocked below the SDK."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from daimon_harness import model_adapter
from daimon_harness.journal import Journal
from daimon_harness.model_adapter import AzureModelAdapter
from daimon_harness.runtime import TurnContext
from mux.drivers.selfhosted.wire import TurnRequest
from openai import AsyncAzureOpenAI

from .conftest import turn


async def test_sdk_tool_loop_meters_each_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "unit-test.openai.azure.com"
        assert request.url.path.endswith("/responses")
        body = json.loads(request.content)
        requests.append(body)
        committed = journal.events("session-1")
        assert committed[-1].type == "model_request_started"
        assert sum(event.type == "model_request_started" for event in committed) == len(requests)
        assert body["store"] is False
        if len(requests) == 1:
            output = [
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "write",
                    "arguments": '{"path":"from-sdk.txt","content":"sdk"}',
                    "status": "completed",
                }
            ]
        else:
            assert any(item.get("type") == "function_call_output" for item in body["input"])
            output = [
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "saved", "annotations": []}],
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": f"resp_{len(requests)}",
                "object": "response",
                "created_at": 1,
                "model": "gpt-5",
                "status": "completed",
                "output": output,
                "usage": {
                    "input_tokens": 12,
                    "input_tokens_details": {"cached_tokens": 3},
                    "output_tokens": 8,
                    "output_tokens_details": {"reasoning_tokens": 2},
                    "total_tokens": 20,
                },
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
            },
        )

    client = AsyncAzureOpenAI(
        azure_endpoint="https://unit-test.openai.azure.com",
        api_key="test",
        api_version="2025-04-01-preview",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        max_retries=0,
    )
    monkeypatch.setattr(model_adapter, "AsyncAzureOpenAI", lambda **kwargs: client)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://unit-test.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test")
    monkeypatch.setenv("HARNESS_CONFIRM_TOOLS", "")
    events: list[tuple[str, object]] = []
    journal = Journal(tmp_path)
    request = TurnRequest.model_validate(turn())
    journal.claim(request)

    def emit(kind, data):
        journal.append(request, kind, data)
        events.append((kind, data))

    context = TurnContext(request, tmp_path, emit)
    history = await AzureModelAdapter().run(context, [])
    assert (tmp_path / "work/from-sdk.txt").read_text() == "sdk"
    assert len(requests) == 2
    usages = [data for kind, data in events if kind == "usage"]
    starts = [data for kind, data in events if kind == "model_request_started"]
    assert [data["request_id"] for data in starts] == [data["request_id"] for data in usages]
    assert usages == [
        {
            "request_id": usages[i - 1]["request_id"],
            "model": "gpt-5",
            "input_tokens": 12,
            "cached_input_tokens": 3,
            "output_tokens": 8,
            "reasoning_tokens": 2,
        }
        for i in (1, 2)
    ]
    assert journal.pending_requests(request) == {}
    journal.close()
    assert len(history) == 4  # user, call, result, assistant
    assert [kind for kind, _ in events].count("tool_use") == 1
    assert [kind for kind, _ in events].count("tool_result") == 1


@pytest.mark.skipif(
    not (
        os.environ.get("AZURE_OPENAI_ENDPOINT")
        and (os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("AZURE_CLIENT_ID"))
    ),
    reason="requires explicit Azure OpenAI credentials",
)
async def test_live_azure_responses(tmp_path: Path) -> None:
    body = turn()
    body["model"] = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5")
    body["input"] = [{"type": "text", "text": "Reply with the word ready. Do not use tools."}]
    events: list[tuple[str, object]] = []
    context = TurnContext(
        TurnRequest.model_validate(body), tmp_path, lambda kind, data: events.append((kind, data))
    )
    await AzureModelAdapter().run(context, [])
    assert sum(kind == "usage" for kind, _ in events) == 1
    assert any(kind == "agent_message" for kind, _ in events)


@pytest.mark.parametrize("failure", ["http", "missing_usage", "cancelled"])
async def test_sdk_unmeasured_request_never_invents_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import asyncio

    from daimon_harness.errors import HarnessError

    started = asyncio.Event()
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        if failure == "cancelled":
            await asyncio.Event().wait()
        if failure == "http":
            return httpx.Response(
                500,
                headers={"x-request-id": "failed-request"},
                json={"error": {"message": "private-error", "type": "server_error"}},
            )
        return httpx.Response(
            200,
            json={
                "id": "missing-usage-request",
                "object": "response",
                "created_at": 1,
                "model": "gpt-5",
                "status": "completed",
                "output": [
                    {
                        "id": "msg",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "done", "annotations": []}],
                    }
                ],
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
            },
        )

    client = AsyncAzureOpenAI(
        azure_endpoint="https://unit-test.openai.azure.com",
        api_key="test",
        api_version="2025-04-01-preview",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        max_retries=0,
    )
    monkeypatch.setattr(model_adapter, "AsyncAzureOpenAI", lambda **kwargs: client)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://unit-test.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test")
    events: list[tuple[str, dict[str, object]]] = []
    context = TurnContext(
        TurnRequest.model_validate(turn()), tmp_path, lambda kind, data: events.append((kind, data))
    )
    task = asyncio.create_task(AzureModelAdapter().run(context, []))
    await asyncio.wait_for(started.wait(), 5)
    if failure == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(HarnessError):
            await task
    assert calls == 1
    assert not any(kind == "usage" for kind, _ in events)
    error = next(data for kind, data in events if kind == "error")
    assert error["kind"] == "usage_unavailable"
    start = next(data for kind, data in events if kind == "model_request_started")
    assert start["request_id"] == error["request_id"]
    assert error["request_id"]
    assert "private-error" not in str(events)
