"""Exercise the pinned SDK over HTTPX; every response is synthetic and offline."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from mux.drivers.gemini.transport import Object, SDKTransport, close_iterator
from mux.errors import ProviderError
from pydantic import JsonValue


@pytest.mark.asyncio
async def test_sdk_submits_documented_antigravity_extra_body_and_reads_steps() -> None:
    captured: list[httpx.Request] = []
    stamp = datetime.now(UTC).isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "i1",
                "created": stamp,
                "updated": stamp,
                "status": "completed",
                "steps": [],
                "environment_id": "e1",
                "usage": {"total_output_tokens": 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        transport = SDKTransport(client)
        response = await transport.create(
            {
                "agent": "antigravity-preview-05-2026",
                "agent_config": {"type": "antigravity", "model": "gemini-3.8-flash"},
                "input": [{"type": "text", "text": "hello"}],
                "environment": "remote",
                "background": True,
                "store": True,
            }
        )
        assert response["id"] == "i1" and response["steps"] == []
        posted = json.loads(captured[0].content)
        assert posted["tools"] == []
        assert posted["agent_config"]["type"] == "antigravity"
        assert posted["input"][0]["text"] == "hello"
        assert captured[0].headers["Api-Revision"] == "2026-05-20"
        assert (await transport.get("i1"))["id"] == "i1"
        await transport.cancel("i1")
        assert captured[1].url.path.endswith("/interactions/i1")
        assert captured[2].url.path.endswith("/interactions/i1/cancel") or captured[
            2
        ].url.path.endswith("/interactions/i1:cancel")
        await client.aio.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "category"),
    [
        (400, "invalid_request"),
        (401, "auth"),
        (403, "permission"),
        (404, "not_found"),
        (409, "conflict"),
        (429, "rate_limited"),
        (503, "overloaded"),
    ],
)
async def test_sdk_errors_are_owned_and_do_not_echo_provider_secrets(
    status: int, category: str
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            status, json={"error": {"message": "SECRET echoed upstream", "code": status}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(client).create(
                {"agent": "antigravity-preview-05-2026", "input": "hello"}
            )
        assert exc.value.category == category
        assert exc.value.native_code == ("503" if status == 503 else None)
        assert "SECRET" not in str(exc.value)
        assert exc.value.__cause__ is None
        assert len(calls) == 1
        await client.aio.aclose()


@pytest.mark.asyncio
async def test_sdk_open_stream_closes_http_response_without_iteration() -> None:
    class TrackedStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"event_type":"step.delta","index":0,"delta":{"type":"text","text":"hi"}}\n\n'

        async def aclose(self) -> None:
            self.closed = True

    body = TrackedStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        opened = await SDKTransport(client).open_stream("i1")
        assert not body.closed
        await close_iterator(opened)
        assert body.closed
        await client.aio.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403, 404, 429, 503])
async def test_sdk_binary_workspace_snapshot_uses_documented_download(status: int) -> None:
    captured: list[httpx.Request] = []
    data = b"\x00\xffraw-snapshot\n"

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return (
            httpx.Response(status, content=data)
            if status == 200
            else httpx.Response(status, json={"error": {"message": "SECRET echo", "code": status}})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        transport = SDKTransport(client)
        if status == 200:
            assert await transport.download_snapshot("e1") == data
        else:
            with pytest.raises(ProviderError) as exc:
                await transport.download_snapshot("e1")
            assert (
                exc.value.category
                == {403: "permission", 404: "not_found", 429: "rate_limited", 503: "overloaded"}[
                    status
                ]
            )
            assert "SECRET" not in str(exc.value) and exc.value.__cause__ is None
        assert len(captured) == 1
        assert captured[0].method == "GET"
        assert captured[0].url.path == "/v1beta/files/environment-e1:download"
        assert captured[0].url.params["alt"] == "media"
        await client.aio.aclose()


@pytest.mark.asyncio
async def test_sdk_interrupted_snapshot_body_is_typed_and_connection_is_closed() -> None:
    class BrokenBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"\x00\xffpartial binary archive"
            raise httpx.ReadError("SECRET echoed in failed body")

        async def aclose(self) -> None:
            self.closed = True

    body = BrokenBody()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(client).download_snapshot("e1")
        assert exc.value.category == "transient_network"
        assert "SECRET" not in str(exc.value) and exc.value.__cause__ is None
        assert len(calls) == 1 and body.closed
        await client.aio.aclose()


@pytest.mark.parametrize("configured", [False, True])
async def test_every_create_wire_body_has_exact_tools_on_continuation_and_fallback(
    configured: bool,
) -> None:
    captured: list[httpx.Request] = []
    tools = [{"type": "code_execution"}] if configured else []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 2:
            return httpx.Response(503, json={"error": {"code": 503, "message": "overloaded"}})
        return httpx.Response(200, json={"id": "next", "status": "completed", "steps": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        source = SDKTransport(client)
        base: Object = {
            "agent": "antigravity-preview-05-2026",
            "agent_config": {"type": "antigravity", "model": "gemini-3.8-flash"},
            "input": [{"type": "text", "text": "hello"}],
            "environment": "remote",
        }
        if configured:
            base["tools"] = [{"type": "code_execution"}]
        await source.create(base)
        continuation: Object = {**base, "previous_interaction_id": "next", "environment": "e1"}
        with pytest.raises(ProviderError, match="overloaded"):
            await source.create(continuation)
        # A caller's explicitly selected 503 fallback traverses the same SDK boundary.
        await source.create(
            {
                **continuation,
                "agent_config": {"type": "antigravity", "model": "gemini-flash-latest"},
            }
        )
        posted = [json.loads(request.content) for request in captured]
        assert len(posted) == 3  # No hidden SDK retry.
        assert [body["tools"] for body in posted] == [tools, tools, tools]
        assert [body.get("previous_interaction_id") for body in posted] == [None, "next", "next"]
        assert posted[-1]["agent_config"]["model"] == "gemini-flash-latest"
        assert ("tools" in base) == configured  # The caller's stored request is not mutated.
        await client.aio.aclose()


@pytest.mark.parametrize("tools", [None, "defaults", {"type": "google_search"}])
async def test_non_list_tools_are_refused_before_native_io(tools: JsonValue) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "unexpected", "status": "completed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture", http_options=HttpOptions(httpx_async_client=http)
        )
        with pytest.raises(ProviderError) as error:
            await SDKTransport(client).create(
                {"agent": "antigravity-preview-05-2026", "tools": tools}
            )
        assert error.value.native_code == "explicit_tools_required"
        assert captured == []
        await client.aio.aclose()
