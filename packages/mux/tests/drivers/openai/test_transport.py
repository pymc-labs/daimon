from __future__ import annotations

import json

import httpx
import pytest
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError
from openai import AsyncOpenAI

from .conftest import REF, SCOPE, native_session, page, turn


@pytest.mark.asyncio
async def test_actual_pinned_sdk_paths_headers_query_body_and_202() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return (
            httpx.Response(202)
            if request.method == "POST"
            else httpx.Response(200, json={"data": [], "has_more": False})
        )

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        transport = SDKTransport(sdk)
        assert (
            await transport.request(
                "POST",
                "/agents/sessions/s/events",
                body={"events": [{"type": "agent.session.input.cancel"}]},
                key="key",
            )
            == {}
        )
        await transport.request(
            "GET", "/agents/sessions/s/items", query={"after": "item", "order": "asc", "limit": 2}
        )
    assert seen[0].url.path == "/v1/agents/sessions/s/events"
    assert seen[0].headers["OpenAI-Beta"] == "agents=v1"
    assert seen[0].headers["Idempotency-Key"] == "key"
    assert "idempotency_key" not in json.loads(seen[0].content)
    assert dict(seen[1].url.params) == {"after": "item", "order": "asc", "limit": "2"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "category"),
    [
        (400, "invalid_request"),
        (401, "auth"),
        (403, "permission"),
        (404, "not_found"),
        (408, "transient_network"),
        (409, "conflict"),
        (422, "invalid_request"),
        (429, "rate_limited"),
        (500, "upstream"),
        (503, "overloaded"),
    ],
)
async def test_errors_are_owned_redacted_and_mutations_never_retry(
    status: int, category: str
) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, json={"error": {"message": "sensitive-upstream-body", "code": "sensitive-code"}}
        )

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        max_retries=5,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(sdk).request("POST", "/agents/sessions/s/events", body={})
    assert exc.value.category == category and calls == 1
    assert "sensitive" not in str(exc.value) and exc.value.__cause__ is None


@pytest.mark.asyncio
async def test_real_sdk_sse_and_close_before_first_event() -> None:
    wire = {"type": "agent.session.idle", "event_id": "idle", "session": {"id": "s"}}

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["stream"] == "true"
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(wire) + "\n\n",
        )

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        source = await SDKTransport(sdk).open_stream("/agents/sessions/s/events")
        assert [item async for item in source] == [wire]
        unopened = await SDKTransport(sdk).open_stream("/agents/sessions/s/events")
        await unopened.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_malformed_reply_and_network_error() -> None:
    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(malformed)),
    ) as sdk:
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(sdk).request("GET", "/agents")
        assert exc.value.native_code == "malformed_response"

    def disconnect(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("sensitive", request=request)

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(disconnect)),
    ) as sdk:
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(sdk).request("POST", "/agents/sessions", body={})
        assert exc.value.category == "transient_network"


@pytest.mark.asyncio
async def test_sse_error_is_owned_and_redacted() -> None:
    wire = {
        "type": "error",
        "event_id": "error",
        "session_id": "s",
        "error": {"type": "server_error", "code": "rate_limit_exceeded", "message": "sensitive"},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["stream"] == "true"
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(wire) + "\n\n",
        )

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        source = await SDKTransport(sdk).open_stream("/agents/sessions/s/events")
        with pytest.raises(ProviderError) as exc:
            [item async for item in source]
        assert exc.value.category == "rate_limited"
        assert "sensitive" not in str(exc.value) and exc.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["send", "steer", "cancel"])
async def test_public_submission_ports_preserve_key_in_real_http_header(mode: str) -> None:
    posts: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if request.url.path.endswith("/turns"):
                return httpx.Response(200, json=page(turn("in_progress")))
            return httpx.Response(
                200, json=native_session("idle" if mode == "send" else "in_progress")
            )
        posts.append(request)
        return httpx.Response(202)

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        max_retries=5,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            authorization=lambda s, k, i: s == SCOPE,
        )
        key = "Stable.Logical-Key:1"
        message = UserMessage(content=(TextPart(text="hello"),))
        for _ in range(2):
            if mode == "cancel":
                await driver.events.cancel(SCOPE, REF, turn_id="root", key=key)
            elif mode == "steer":
                await driver.events.steer(SCOPE, REF, message, active_turn="root", key=key)
            else:
                await driver.events.send(SCOPE, REF, (message,), key=key)
    assert len(posts) == 2 and posts[0].content == posts[1].content
    assert all(request.headers["Idempotency-Key"] == key for request in posts)
    assert all("idempotency_key" not in json.loads(request.content) for request in posts)
