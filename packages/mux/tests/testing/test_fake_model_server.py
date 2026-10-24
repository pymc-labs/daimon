"""The fake model server through the real OpenAI and Anthropic SDK clients, no network."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from anthropic import APIStatusError as AnthropicStatusError
from anthropic import AsyncAnthropic
from mux.testing.fake_model_server import (
    FakeModelServer,
    Usage,
    call_tool,
    fail,
    say,
)
from openai import APIStatusError, AsyncAzureOpenAI, AsyncOpenAI, RateLimitError

USAGE = Usage(input_tokens=100, cached_input_tokens=40, output_tokens=7, reasoning_tokens=3)


@pytest.fixture
def server() -> FakeModelServer:
    return FakeModelServer()


@pytest.fixture
async def http(server: FakeModelServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://fake"
    ) as client:
        yield client


@pytest.fixture
def openai_client(http: httpx.AsyncClient) -> AsyncOpenAI:
    return AsyncOpenAI(base_url="http://fake/v1", api_key="test", http_client=http, max_retries=0)


@pytest.fixture
def azure_client(http: httpx.AsyncClient) -> AsyncAzureOpenAI:
    return AsyncAzureOpenAI(
        azure_endpoint="http://fake",
        api_version="2025-04-01-preview",
        api_key="test",
        http_client=http,
        max_retries=0,
    )


@pytest.fixture
def anthropic_client(http: httpx.AsyncClient) -> AsyncAnthropic:
    return AsyncAnthropic(base_url="http://fake", api_key="test", http_client=http, max_retries=0)


async def test_responses_text_usage_and_request_record(
    server: FakeModelServer, openai_client: AsyncOpenAI
) -> None:
    server.script([say("hello there", usage=USAGE)])
    response = await openai_client.responses.create(
        model="m",
        instructions="be brief",
        input="hi",
        tools=[{"type": "function", "name": "write", "parameters": {}, "strict": False}],
    )
    assert response.output_text == "hello there"
    assert response.usage is not None
    assert response.usage.input_tokens == 100
    assert response.usage.input_tokens_details.cached_tokens == 40
    assert response.usage.output_tokens_details.reasoning_tokens == 3
    [recorded] = server.requests
    assert recorded.api == "responses"
    assert recorded.instructions == "be brief"
    assert recorded.tool_names == ["write"]
    assert recorded.history == [{"role": "user", "content": "hi"}]
    assert recorded.headers["authorization"] == "<redacted>"


async def test_responses_function_call_non_streaming(
    server: FakeModelServer, openai_client: AsyncOpenAI
) -> None:
    server.script([call_tool("write", {"path": "a", "content": "b"}, call_id="c1")])
    response = await openai_client.responses.create(model="m", input="go")
    [item] = response.output
    assert item.type == "function_call"
    assert (item.name, item.call_id, json.loads(item.arguments)) == (
        "write",
        "c1",
        {"path": "a", "content": "b"},
    )


async def test_responses_streaming_matches_non_streaming(
    server: FakeModelServer, openai_client: AsyncOpenAI
) -> None:
    server.script([call_tool("bash", {"command": "ls"}, text="running", usage=USAGE)])
    stream = await openai_client.responses.create(model="m", input="go", stream=True)
    kinds = [event.type async for event in stream]
    assert kinds[0] == "response.created" and kinds[-1] == "response.completed"
    assert "response.output_text.delta" in kinds
    assert "response.function_call_arguments.done" in kinds
    server.script([call_tool("bash", {"command": "ls"}, text="running", usage=USAGE)])
    async with openai_client.responses.stream(model="m", input="go") as managed:
        final = await managed.get_final_response()
    assert final.output_text == "running"
    assert [item.type for item in final.output] == ["message", "function_call"]
    assert final.usage is not None and final.usage.output_tokens == 7
    assert [r.stream for r in server.requests] == [True, True]


async def test_azure_deployment_url_shapes(
    server: FakeModelServer, azure_client: AsyncAzureOpenAI
) -> None:
    server.script([say("a"), say("b")])
    await azure_client.responses.create(model="my-deployment", input="x")
    completion = await azure_client.chat.completions.create(
        model="my-deployment", messages=[{"role": "user", "content": "x"}]
    )
    assert completion.choices[0].message.content == "b"
    responses_call, chat_call = server.requests
    assert responses_call.path == "/openai/responses"
    assert responses_call.query == {"api-version": "2025-04-01-preview"}
    assert chat_call.path == "/openai/deployments/my-deployment/chat/completions"
    assert responses_call.headers["api-key"] == "<redacted>"


async def test_chat_completions_tool_call_and_usage(
    server: FakeModelServer, openai_client: AsyncOpenAI
) -> None:
    server.script([call_tool("lookup", {"q": "x"}, call_id="c9", usage=USAGE)])
    completion = await openai_client.chat.completions.create(
        model="m",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
        ],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
    )
    choice = completion.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.tool_calls is not None
    call = choice.message.tool_calls[0]
    assert call.type == "function"
    assert (call.id, call.function.name) == ("c9", "lookup")
    assert completion.usage is not None
    assert completion.usage.prompt_tokens_details is not None
    assert completion.usage.prompt_tokens_details.cached_tokens == 40
    [recorded] = server.requests
    assert recorded.instructions == "sys"
    assert recorded.tool_names == ["lookup"]


async def test_chat_completions_streaming_accumulates(
    server: FakeModelServer, openai_client: AsyncOpenAI
) -> None:
    server.script([call_tool("lookup", {"q": "x"}, text="one two", usage=USAGE)])
    stream = await openai_client.chat.completions.create(
        model="m",
        messages=[{"role": "user", "content": "go"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    text, arguments, usage = "", "", None
    async for chunk in stream:
        if chunk.usage:
            usage = chunk.usage
        for choice in chunk.choices:
            text += choice.delta.content or ""
            for call in choice.delta.tool_calls or []:
                arguments += (call.function.arguments if call.function else "") or ""
    assert text == "one two"
    assert json.loads(arguments) == {"q": "x"}
    assert usage is not None and usage.completion_tokens == 7


async def test_anthropic_messages_non_streaming(
    server: FakeModelServer, anthropic_client: AsyncAnthropic
) -> None:
    server.script([call_tool("calc", {"n": 2}, text="thinking", call_id="tu1", usage=USAGE)])
    message = await anthropic_client.messages.create(
        model="claude-x",
        max_tokens=10,
        system="sys prompt",
        messages=[{"role": "user", "content": "go"}],
        tools=[{"name": "calc", "description": "d", "input_schema": {"type": "object"}}],
    )
    assert message.stop_reason == "tool_use"
    assert [block.type for block in message.content] == ["text", "tool_use"]
    assert message.content[1].input == {"n": 2}  # type: ignore[union-attr]
    assert message.usage.input_tokens == 60
    assert message.usage.cache_read_input_tokens == 40
    [recorded] = server.requests
    assert recorded.api == "anthropic_messages"
    assert recorded.instructions == "sys prompt"
    assert recorded.tool_names == ["calc"]
    assert recorded.headers["x-api-key"] == "<redacted>"


async def test_anthropic_messages_streaming(
    server: FakeModelServer, anthropic_client: AsyncAnthropic
) -> None:
    server.script([call_tool("calc", {"n": 2}, text="hi there", usage=USAGE)])
    async with anthropic_client.messages.stream(
        model="claude-x", max_tokens=10, messages=[{"role": "user", "content": "go"}]
    ) as stream:
        final = await stream.get_final_message()
    assert [block.type for block in final.content] == ["text", "tool_use"]
    assert final.content[0].text == "hi there"  # type: ignore[union-attr]
    assert final.content[1].input == {"n": 2}  # type: ignore[union-attr]
    assert final.usage.output_tokens == 7


async def test_scripted_errors_use_each_api_error_shape(
    server: FakeModelServer, openai_client: AsyncOpenAI, anthropic_client: AsyncAnthropic
) -> None:
    server.script([fail(429, "slow down", retry_after_s=2), fail(500, "boom"), fail(529)])
    with pytest.raises(RateLimitError) as limited:
        await openai_client.responses.create(model="m", input="x")
    assert limited.value.response.headers["retry-after"] == "2.0"
    with pytest.raises(APIStatusError) as broken:
        await openai_client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "x"}]
        )
    assert broken.value.status_code == 500 and broken.value.body == {
        "message": "boom",
        "type": "server_error",
        "param": None,
        "code": "server_error",
    }
    with pytest.raises(AnthropicStatusError) as overloaded:
        await anthropic_client.messages.create(
            model="c", max_tokens=1, messages=[{"role": "user", "content": "x"}]
        )
    assert overloaded.value.status_code == 529


async def test_conversations_select_script_by_header_or_model(
    server: FakeModelServer, http: httpx.AsyncClient
) -> None:
    server.script([say("by-model")], conversation="model-a")
    server.script([say("by-header")], conversation="named")
    server.script([say("fallback")])
    by_model = await http.post("/v1/responses", json={"model": "model-a", "input": "x"})
    by_header = await http.post(
        "/v1/responses",
        json={"model": "model-a", "input": "x"},
        headers={"x-fake-conversation": "named"},
    )
    fallback = await http.post("/v1/responses", json={"model": "unknown", "input": "x"})
    texts = [r.json()["output"][0]["content"][0]["text"] for r in (by_model, by_header, fallback)]
    assert texts == ["by-model", "by-header", "fallback"]
    assert [r.conversation for r in server.requests] == ["model-a", "named", "default"]


async def test_exhausted_script_fails_loudly_and_is_recorded(
    server: FakeModelServer, http: httpx.AsyncClient
) -> None:
    response = await http.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert response.status_code == 500
    assert "exhausted" in response.json()["error"]["message"]
    assert server.requests[0].answered_with == "exhausted"


async def test_delay_and_wait_for_requests(
    server: FakeModelServer, http: httpx.AsyncClient
) -> None:
    import asyncio

    server.script([say("slow", delay_s=0.2)])
    pending = asyncio.create_task(http.post("/v1/responses", json={"model": "m", "input": "x"}))
    [seen] = await server.wait_for_requests(1, timeout_s=2)
    assert not pending.done() and seen.answered_with == "reply"
    assert (await pending).status_code == 200


async def test_control_surface_scripts_and_reads_back(http: httpx.AsyncClient) -> None:
    steps = [
        {"kind": "reply", "text": "remote", "usage": {"input_tokens": 1}},
        {"kind": "fail", "status": 503},
    ]
    scripted = await http.post("/_fake/scripts?conversation=c1", json=steps)
    assert scripted.json() == {"remaining": 2}
    first = await http.post("/v1/responses", headers={"x-fake-conversation": "c1"}, json={})
    second = await http.post("/v1/responses", headers={"x-fake-conversation": "c1"}, json={})
    assert (first.status_code, second.status_code) == (200, 503)
    recorded = (await http.get("/_fake/requests?conversation=c1")).json()
    assert [item["answered_with"] for item in recorded] == ["reply", "fail"]
    assert (await http.post("/_fake/reset")).json() == {"ok": True}
    assert (await http.get("/_fake/requests")).json() == []


async def test_unknown_route_and_method(http: httpx.AsyncClient) -> None:
    assert (await http.get("/nope")).status_code == 404
    assert (await http.get("/v1/responses")).status_code == 405


async def test_responses_instructions_include_leading_developer_messages(
    server: FakeModelServer, openai_client: AsyncOpenAI
) -> None:
    server.script([say("ok")])
    await openai_client.responses.create(
        model="m",
        instructions="field text",
        input=[
            {"role": "developer", "content": "developer text"},
            {"role": "user", "content": "user text"},
        ],
    )
    [recorded] = server.requests
    assert recorded.instructions == "field text\ndeveloper text"
