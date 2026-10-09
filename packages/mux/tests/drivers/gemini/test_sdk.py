"""Exercise the pinned SDK over HTTPX; every response is synthetic and offline."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from google import genai
from mux.drivers.gemini.transport import SDKTransport
from mux.errors import ProviderError


@pytest.mark.asyncio
async def test_sdk_submits_documented_antigravity_extra_body_and_reads_steps():
    captured = []
    stamp = datetime.now(UTC).isoformat()

    def handler(request):
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
            http_options={"httpx_async_client": http, "retry_options": {"attempts": 1}},
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
async def test_sdk_errors_are_owned_and_do_not_echo_provider_secrets(status, category):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, json={"error": {"message": "SECRET echoed upstream", "code": status}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options={"httpx_async_client": http, "retry_options": {"attempts": 1}},
        )
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(client).create(
                {"agent": "antigravity-preview-05-2026", "input": "hello"}
            )
        assert exc.value.category == category
        assert "SECRET" not in str(exc.value)
        assert exc.value.__cause__ is None
        assert len(calls) == 1
        await client.aio.aclose()


@pytest.mark.asyncio
async def test_sdk_open_stream_closes_http_response_without_iteration():
    class TrackedStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'data: {"event_type":"step.delta","index":0,"delta":{"type":"text","text":"hi"}}\n\n'

        async def aclose(self):
            self.closed = True

    body = TrackedStream()

    async def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options={"httpx_async_client": http, "retry_options": {"attempts": 1}},
        )
        opened = await SDKTransport(client).open_stream("i1")
        assert not body.closed
        await opened.aclose()
        assert body.closed
        await client.aio.aclose()
